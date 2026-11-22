"""The reserved pull request link of a loop-managed item.

Only ``ledger.loop.link_pull_request`` writes an ``item.linked`` with rel
``pr`` on a loop-managed item: through the store's one door
(``Ledger._append_loop_pr_link``), as the holder of the item's live claim,
naming one pull request in the one stored form (``receipts.pr_url``), while
the item has no other ``pr`` link and no other item links that pull request
by the reader's comparison (``receipts.pr_item``). Every refusal here also
checks that nothing was written.

The module reaches the writer through ``ledger.loop`` at call time, so the
tests of what stays as it is (``test_g*``) also run against a ledger that has
no such writer.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ledger import lease, receipts
from ledger import loop
from ledger.card import project_card
from ledger.cli import main
from ledger.export import (
    event_filename,
    event_record,
    export_events,
    hash_body,
    verify_export,
    write_stream_digest,
)
from ledger.loop import (
    _LOOP_WRITER_CAPABILITY,
    claim_operation,
    close_item,
    finish_operation,
    manage_item,
    open_hand_off,
    take_item,
)
from ledger.restore import restore_export
from ledger.schema import ValidationError
from ledger.store import (
    GENESIS_HASH,
    Event,
    IdempotencyConflict,
    canonical_dumps,
    claim_database,
    event_digest,
    open_ledger,
)
from ledger.verbs import CliError

from receipt_helpers import DOC, fields as receipt_fields

ITEM = "row-1"
OTHER = "plain-1"
HOLDER = "loop-1"
SUCCESSOR = "loop-2"
REPO = "example/repo"
PR = 7
URL = "https://github.com/example/repo/pull/7"
URL_8 = "https://github.com/example/repo/pull/8"
KEY = f"loop:{ITEM}:pr-link:{URL}"
MANAGE_KEY = f"loop:{ITEM}:managed"
HEAD = "a" * 40
MERGE = {"key": f"merge:example/repo#7:{HEAD}", "pr": URL, "head": HEAD}
SEAMS = {"build": "omp-builder", "review": "review-gate", "outward": "draft-pr"}


def invoke(monkeypatch, db: str, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--db", db, *args])
    return code, out.getvalue(), err.getvalue()


def new_item(ledger, item_id: str) -> None:
    ledger.append(
        kind="item.created",
        payload={"type": "ticket", "title": item_id},
        item_id=item_id,
        idempotency_key=f"created:{item_id}",
    )


def taken(tmp_path: Path, name: str = "ledger.db", holder: str = HOLDER) -> str:
    """ITEM loop-managed under ``holder``'s live (stamped) claim, and OTHER plain."""
    path = str(tmp_path / name)
    with open_ledger(path) as ledger:
        new_item(ledger, ITEM)
        new_item(ledger, OTHER)
        take_item(ledger, ITEM, holder, idempotency_key=MANAGE_KEY, claim_key=f"take:{ITEM}:1")
    return path


@pytest.fixture
def db(tmp_path: Path) -> str:
    """ITEM loop-managed and handed off: HOLDER's claim, an open hand-off."""
    path = taken(tmp_path)
    with open_ledger(path, create=False) as ledger:
        claim_operation(
            ledger, ITEM, "build", "build-1", holder=HOLDER, hand_off=True,
            controller="director", seams=dict(SEAMS),
        )
        assert open_hand_off(ledger, ITEM) is not None
    return path


def all_events(db: str) -> list[Event]:
    with open_ledger(db, create=False) as ledger:
        return ledger.events()


def link(ledger, **overrides):
    kwargs = {"item_id": ITEM, "holder": HOLDER, "repo": REPO, "pr": PR}
    kwargs.update(overrides)
    return loop.link_pull_request(
        ledger, kwargs.pop("item_id"), kwargs.pop("holder"), kwargs.pop("repo"),
        kwargs.pop("pr"), **kwargs,
    )


def assert_refused(db: str, error: type[BaseException], name: str, **overrides) -> BaseException:
    """The link is refused with ``name`` as the message's start, and writes nothing."""
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(error, match=f"^{re.escape(name)}") as caught:
            link(ledger, **overrides)
        assert not ledger._conn.in_transaction
    assert all_events(db) == before, "a refused link must write nothing"
    return caught.value


def pr_links(db: str, item_id: str = ITEM) -> list[Event]:
    return [
        event for event in all_events(db)
        if event.kind == "item.linked" and event.item_id == item_id
        and event.payload["rel"] == "pr"
    ]


def expire(ledger, holder: str = HOLDER) -> None:
    """``holder``'s claim, rewritten with a lease that ran out an hour ago."""
    ledger.append(
        kind="item.state",
        payload={
            "status": "claimed",
            "claim": holder,
            "expires_at": lease.stamp(lease.now() - timedelta(hours=1)),
        },
        item_id=ITEM,
        idempotency_key=f"expire:{holder}",
    )


def write_chain_export(export_dir: Path, events: list[tuple[str, str, dict]]) -> None:
    """Write a valid hash chain of ``(item, kind, payload)``, payload version 1."""
    export_dir.mkdir()
    previous = GENESIS_HASH
    for event_id, (item_id, kind, payload) in enumerate(events, 1):
        event = Event(
            id=event_id,
            kind=kind,
            payload_version=1,
            payload=payload,
            item_id=item_id,
            idempotency_key=f"chain-{event_id}",
            actor=None,
            harness="pytest",
            model=None,
            session=None,
            channel=None,
            provenance_class=None,
            recorded_at=f"2026-01-01T00:00:0{event_id}.000Z",
            prev_hash=previous,
            hash="",
        )
        event = replace(event, hash=event_digest(hash_body(event)))
        (export_dir / event_filename(event_id)).write_text(
            canonical_dumps(event_record(event)) + "\n", encoding="utf-8"
        )
        previous = event.hash
    (export_dir / "watermark.json").write_text(
        json.dumps({"id": len(events)}, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    write_stream_digest(export_dir, verify_export(export_dir))


# --- F: the feature (red on a ledger without the writer) ---------------------


def test_f1_the_holder_writes_one_ordinary_pr_link_that_the_readers_match(db: str) -> None:
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        result = link(ledger)
    assert isinstance(result, loop.PullRequestLink)
    assert result.created is True
    assert all_events(db)[len(before):] == [result.event]
    event = result.event
    assert (event.kind, event.payload_version, event.item_id) == ("item.linked", 1, ITEM)
    assert event.payload == {"rel": "pr", "target": receipts.pr_url(REPO, PR)}
    assert event.payload["target"] == URL
    assert event.idempotency_key == KEY
    with open_ledger(db, create=False) as ledger:
        # The reader finds the item by the URL, and by the URL in another case.
        assert receipts.pr_item(ledger, URL) == ITEM
        assert receipts.pr_item(ledger, "https://github.com/Example/Repo/pull/7") == ITEM
        receipt = receipts.write_receipt(ledger, ITEM, receipt_fields(repo=REPO, pr=PR), DOC)
        assert (receipt.kind, receipt.item_id) == ("review.receipt", ITEM)
        merge = receipts.write_merge_intent(ledger, ITEM, MERGE)
        assert (merge.created, merge.event.item_id) == (True, ITEM)
        written = len(ledger.events())
        for call in (
            lambda: receipts.write_receipt(
                ledger, OTHER, receipt_fields(repo=REPO, pr=PR), DOC
            ),
            lambda: receipts.write_merge_intent(ledger, OTHER, MERGE),
        ):
            with pytest.raises(CliError) as caught:
                call()
            assert caught.value.code == "receipt_item_mismatch"
        assert len(ledger.events()) == written


def test_f2_no_hand_off_is_needed(tmp_path: Path) -> None:
    # A managed item with only a version 1 build intent.
    plain = taken(tmp_path, "plain.db")
    with open_ledger(plain, create=False) as ledger:
        claim_operation(ledger, ITEM, "build", "build-1", holder=HOLDER)
        assert open_hand_off(ledger, ITEM) is None
        assert link(ledger).created is True
    assert [event.payload["target"] for event in pr_links(plain)] == [URL]

    # A hand-off with its loop.done, then a new holder's claim (a takeover).
    done = taken(tmp_path, "done.db")
    with open_ledger(done, create=False) as ledger:
        claim_operation(
            ledger, ITEM, "build", "build-1", holder=HOLDER, hand_off=True,
            controller="director", seams=dict(SEAMS),
        )
        finish_operation(ledger, ITEM, "build", "build-1", holder=HOLDER)
        assert open_hand_off(ledger, ITEM) is None
        expire(ledger)
        take_item(ledger, ITEM, SUCCESSOR, idempotency_key=MANAGE_KEY, claim_key="take:2")
        assert link(ledger, holder=SUCCESSOR).created is True
    assert [event.payload["target"] for event in pr_links(done)] == [URL]


def test_f3_the_same_pull_request_again_returns_the_stored_link(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        first = link(ledger, actor="seat-a", harness="h-a", model="m-a", session="s-a")
    assert first.created is True
    assert first.event.idempotency_key == KEY
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        again = link(ledger, actor="seat-a", harness="h-a", model="m-a", session="s-a")
        assert (again.created, again.event) == (False, first.event)
        # Another actor and session: the retry stamps are not compared.
        stamped = link(ledger, actor="seat-b", harness="h-b", model="m-b", session="s-b")
        assert (stamped.created, stamped.event) == (False, first.event)
        # A caller that no longer holds the claim: no claim is re-read on a retry.
        stranger = link(ledger, holder="not-the-holder")
        assert (stranger.created, stranger.event) == (False, first.event)
    assert all_events(db) == before, "a retry must write nothing"


def test_f4_the_readers_show_and_keep_the_link(db: str, tmp_path: Path, monkeypatch) -> None:
    claim_database(db, "home")
    with open_ledger(db, create=False) as ledger:
        written = link(ledger).event

    code, out, err = invoke(monkeypatch, db, "--json", "links", ITEM)
    assert code == 0, err
    assert [(row["rel"], row["target"]) for row in json.loads(out)["data"]["outgoing"]
            if row["rel"] == "pr"] == [("pr", URL)]
    code, out, err = invoke(monkeypatch, db, "--json", "card", ITEM)
    assert code == 0, err
    assert [(row["rel"], row["target"]) for row in json.loads(out)["data"]["links"]
            if row["rel"] == "pr"] == [("pr", URL)]
    code, out, err = invoke(monkeypatch, db, "--json", "export-item", ITEM)
    assert code == 0, err
    records = [record for record in json.loads(out)["data"]["events"]
               if record["kind"] == "item.linked"]
    assert [(r["payload"], r["payload_version"]) for r in records] == [
        ({"rel": "pr", "target": URL}, 1)
    ]
    code, _out, err = invoke(monkeypatch, db, "verify")
    assert code == 0, err

    export_dir = tmp_path / "export"
    with open_ledger(db, create=False) as ledger:
        export_events(ledger, export_dir)
        original = ledger.events()
    exported = [event for event in verify_export(export_dir) if event.kind == "item.linked"]
    assert [(e.id, e.payload, e.idempotency_key) for e in exported] == [
        (written.id, {"rel": "pr", "target": URL}, KEY)
    ]
    dest = tmp_path / "restored.db"
    restore_export(export_dir, dest, system="home", accept_loop_events=True)
    with open_ledger(dest, create=False) as restored:
        restored.verify(system="home")
        assert restored.events()[: len(original)] == original
        assert restored.event_by_key(KEY) == written
        assert receipts.pr_item(restored, URL) == ITEM

    # A build result naming the pull request in the one stored form
    # (receipts.pr_url) is the stored target, so the card has one pr row.
    with open_ledger(db, create=False) as ledger:
        finish_operation(
            ledger, ITEM, "build", "build-1", holder=HOLDER, target=receipts.pr_url(REPO, PR)
        )
        rows = [(row["event_id"], row["target"]) for row in project_card(ledger, ITEM).links
                if row["rel"] == "pr"]
    assert rows == [(written.id, URL)]


def test_f5_a_successor_after_a_takeover_retries_and_cannot_relink(tmp_path: Path) -> None:
    path = taken(tmp_path)
    with open_ledger(path, create=False) as ledger:
        first = link(ledger)
        expire(ledger)
        claim = take_item(
            ledger, ITEM, SUCCESSOR, idempotency_key=MANAGE_KEY, claim_key="take:2"
        )
        assert claim.payload["claim"] == SUCCESSOR
        assert claim.payload["prior_holder"] == HOLDER
        again = link(ledger, holder=SUCCESSOR)
        assert (again.created, again.event) == (False, first.event)
    assert_refused(
        path, ValidationError, f"loop-pr-link-already-exists:{ITEM}", holder=SUCCESSOR, pr=8
    )
    assert pr_links(path) == [first.event]


def test_f6_the_loop_finishes_on_a_linked_item(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        written = link(ledger).event
        target = written.payload["target"]
        finish_operation(ledger, ITEM, "build", "build-1", holder=HOLDER, target=target)
        claim_operation(ledger, ITEM, "merge", "merge-1", holder=HOLDER, target=target)
        done = finish_operation(ledger, ITEM, "merge", "merge-1", holder=HOLDER, target=target)
        closed = close_item(
            ledger, ITEM, done.id, idempotency_key=f"loop:{ITEM}:closed", holder=HOLDER
        )
        assert closed.kind == "loop.closed"
        assert ledger.item(ITEM)["status"] == "closed"
        assert receipts.pr_item(ledger, URL) == ITEM
    assert pr_links(db) == [written]
    error = assert_refused(db, lease.ClaimRefused, "", pr=9)
    assert error.code == "claim_lost"
    assert str(error).endswith("pull request link not written")


# --- I: concurrent writers ----------------------------------------------------

_LIB = Path(__file__).resolve().parents[2]
RACER = r"""
import sys, time
from pathlib import Path
from ledger import open_ledger
from ledger.loop import link_pull_request
from ledger.schema import ValidationError

db, pr, go = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
deadline = time.monotonic() + 30
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.001)
with open_ledger(db, create=False) as ledger:
    try:
        result = link_pull_request(ledger, "row-1", "loop-1", "example/repo", pr)
        print("created" if result.created else "retried")
    except ValidationError as exc:
        print("refused " + str(exc))
"""


def race(tmp_path: Path, db: str, prs: list[int]) -> list[str]:
    """One writer process per pull request number, released together; each
    one's outcome line, in the order given."""
    script = tmp_path / "racer.py"
    script.write_text(RACER, encoding="utf-8")
    go = tmp_path / "go"
    env = dict(os.environ, PYTHONPATH=str(_LIB) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), db, str(pr), str(go)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for pr in prs
    ]
    go.write_text("go\n", encoding="utf-8")
    outcomes = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
        outcomes.append(stdout.strip())
    return outcomes


def test_i1_two_writers_racing_two_pull_requests_leave_one_link(db: str, tmp_path: Path) -> None:
    outcomes = race(tmp_path, db, [7, 8])
    assert sorted(outcome.split(" ")[0] for outcome in outcomes) == ["created", "refused"]
    winner = [URL, URL_8][outcomes.index("created")]
    loser = outcomes[1 - outcomes.index("created")]
    assert loser == f"refused loop-pr-link-already-exists:{ITEM}"
    assert [event.payload["target"] for event in pr_links(db)] == [winner]


def test_i2_two_writers_racing_one_pull_request_write_it_once(db: str, tmp_path: Path) -> None:
    before = len(all_events(db))
    outcomes = race(tmp_path, db, [7, 7])
    assert sorted(outcomes) == ["created", "retried"]
    written = all_events(db)[before:]
    assert [(event.kind, event.payload) for event in written] == [
        ("item.linked", {"rel": "pr", "target": URL})
    ]


# --- G: what stays as it is (green on a ledger without the writer) ------------


@pytest.mark.parametrize(
    "writer",
    ["cli-link-url", "cli-link-short", "cli-append", "cli-append-many", "append", "append-many"],
)
def test_g1_every_generic_writer_is_refused_a_pr_link(
    tmp_path: Path, monkeypatch, writer: str
) -> None:
    db = taken(tmp_path)
    before = all_events(db)
    payload = {"rel": "pr", "target": URL}
    if writer.startswith("cli-"):
        if writer == "cli-link-url":
            args = ["link", ITEM, "--rel", "pr", "--target", URL]
        elif writer == "cli-link-short":
            args = ["link", ITEM, "--rel", "pr", "--target", "example/repo#7"]
        elif writer == "cli-append":
            args = ["append", "--kind", "item.linked", "--item", ITEM,
                    "--payload", json.dumps(payload)]
        else:
            args = ["append-many", ITEM]
        stdin = (json.dumps([{"kind": "item.linked", "payload": payload}])
                 if writer == "cli-append-many" else None)
        code, _out, err = invoke(monkeypatch, db, *args, stdin=stdin)
        assert (code, "reserved-transition:loop-pr" in err) == (3, True), (
            f"{writer} exited {code}, stderr {err!r}: the generic pr link was not refused"
        )
    else:
        spec = {"kind": "item.linked", "payload": payload, "item_id": ITEM,
                "idempotency_key": KEY}
        with open_ledger(db, create=False) as ledger:
            with pytest.raises(ValidationError, match="^reserved-transition:loop-pr$"):
                if writer == "append":
                    ledger.append(**spec)
                else:
                    ledger.append_many([spec])
    assert all_events(db) == before


def test_g2_the_internal_loop_path_still_refuses_item_linked(tmp_path: Path) -> None:
    db = taken(tmp_path)
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(ValidationError, match="^internal-kind-required:item.linked$"):
            ledger._append_loop_event(
                _LOOP_WRITER_CAPABILITY,
                kind="item.linked",
                payload={"rel": "pr", "target": URL},
                item_id=ITEM,
                idempotency_key=KEY,
            )
    assert all_events(db) == before


@pytest.mark.parametrize(
    ("rel", "target"),
    [("url", "https://example.com/doc"), ("branch", "feat/x")],
)
def test_g4_a_generic_link_of_another_rel_still_lands(tmp_path: Path, rel: str, target: str) -> None:
    db = taken(tmp_path)
    with open_ledger(db, create=False) as ledger:
        event = ledger.append(
            kind="item.linked",
            payload={"rel": rel, "target": target},
            item_id=ITEM,
            idempotency_key=f"generic:{rel}",
        )
    assert all_events(db)[-1] == event


def test_g5_a_linked_managed_item_cannot_be_managed_again_under_another_key(
    tmp_path: Path,
) -> None:
    # The link is seeded through a restored chain, so this holds without the writer.
    export_dir = tmp_path / "chain"
    write_chain_export(
        export_dir,
        [
            (ITEM, "item.created", {"type": "ticket", "title": "Restored loop item"}),
            (ITEM, "loop.managed", {}),
            (ITEM, "item.state", {"status": "claimed", "claim": HOLDER}),
            (ITEM, "item.linked", {"rel": "pr", "target": URL}),
        ],
    )
    dest = tmp_path / "restored.db"
    restore_export(export_dir, dest, system="home", accept_loop_events=True)
    before = all_events(str(dest))
    with open_ledger(dest, create=False) as ledger:
        with pytest.raises(ValidationError, match=f"^loop-pr-link-already-exists:{ITEM}$"):
            manage_item(ledger, ITEM, idempotency_key="manage-again")
    assert all_events(str(dest)) == before


# --- R: every refusal by name, each writing nothing ---------------------------


@pytest.mark.parametrize("holder", ["", None, 7], ids=["empty", "none", "not-a-string"])
def test_r1_a_holder_is_required(db: str, holder) -> None:
    error = assert_refused(db, ValidationError, "loop-pr-link-needs-holder", holder=holder)
    assert str(error) == "loop-pr-link-needs-holder"


@pytest.mark.parametrize(
    "repo",
    [
        "Example/repo",
        "example/Repo",
        "example",
        "a/b/c",
        "",
        "exa mple/repo",
        "example/repo\n",
        "example/re\npo",
        "еxample/repo",  # CYRILLIC SMALL LETTER IE, a look-alike of "e"
        7,
        None,
    ],
    ids=["upper-owner", "upper-repo", "no-slash", "two-slashes", "empty", "space",
         "trailing-newline", "line-break", "cyrillic", "integer", "none"],
)
def test_r2_repo_off_the_receipt_rule(db: str, repo) -> None:
    error = assert_refused(db, ValidationError, "bad-type:repo", repo=repo)
    assert str(error) == "bad-type:repo"


def test_r2_repo_over_200_characters(db: str) -> None:
    error = assert_refused(db, ValidationError, "too-long:repo", repo="a/" + "b" * 199)
    assert str(error) == "too-long:repo; max 200, got 201"


def test_r2_a_repo_of_200_characters_is_accepted(db: str) -> None:
    repo = "a/" + "b" * 198
    with open_ledger(db, create=False) as ledger:
        result = link(ledger, repo=repo)
    assert result.created is True
    assert result.event.payload["target"] == f"https://github.com/{repo}/pull/7"


@pytest.mark.parametrize(
    "pr",
    [0, -1, "7", 7.0, True, None],
    ids=["zero", "negative", "string", "float", "boolean", "none"],
)
def test_r3_pr_off_the_receipt_rule(db: str, pr) -> None:
    error = assert_refused(db, ValidationError, "bad-type:pr", pr=pr)
    assert str(error) == "bad-type:pr"


def test_r3_pr_above_the_integer_ceiling(db: str) -> None:
    error = assert_refused(db, ValidationError, "too-large:pr", pr=2**63)
    assert str(error) == "too-large:pr"


def test_r3_the_integer_ceiling_is_accepted(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        result = link(ledger, pr=2**63 - 1)
        assert result.created is True
        url = receipts.pr_url(REPO, 2**63 - 1)
        assert receipts.pr_item(ledger, url) == ITEM


def test_r4_an_item_that_is_not_loop_managed(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        # HOLDER's own live claim on the plain item, so only the missing
        # loop.managed refuses.
        ledger.append(
            kind="item.state",
            payload={"status": "claimed", "claim": HOLDER, "expires_at": lease.expires_at(3600)},
            item_id=OTHER,
            idempotency_key=f"claim:{OTHER}",
        )
    error = assert_refused(db, ValidationError, f"loop-item-not-managed:{OTHER}", item_id=OTHER)
    assert str(error) == f"loop-item-not-managed:{OTHER}"
    assert_refused(db, ValidationError, "loop-item-not-managed:no-such-item", item_id="no-such-item")


def _no_claim(tmp_path: Path) -> str:
    path = str(tmp_path / "unclaimed.db")
    with open_ledger(path) as ledger:
        new_item(ledger, ITEM)
        manage_item(ledger, ITEM, idempotency_key=MANAGE_KEY)
    return path


def _other_holder(tmp_path: Path) -> str:
    return taken(tmp_path, "other.db", holder="other-seat")


def _expired(tmp_path: Path) -> str:
    path = taken(tmp_path, "expired.db")
    with open_ledger(path, create=False) as ledger:
        expire(ledger)
    return path


def _closed(tmp_path: Path) -> str:
    path = taken(tmp_path, "closed.db")
    with open_ledger(path, create=False) as ledger:
        claim_operation(ledger, ITEM, "merge", "merge-1", holder=HOLDER)
        done = finish_operation(ledger, ITEM, "merge", "merge-1", holder=HOLDER)
        close_item(ledger, ITEM, done.id, idempotency_key=f"loop:{ITEM}:closed", holder=HOLDER)
    return path


@pytest.mark.parametrize(
    "setup",
    [_no_claim, _other_holder, _expired, _closed],
    ids=["no-claim", "other-holder", "expired", "closed"],
)
def test_r5_only_the_holder_of_the_live_claim_links(tmp_path: Path, setup) -> None:
    db = setup(tmp_path)
    error = assert_refused(db, lease.ClaimRefused, "")
    assert error.code == "claim_lost"
    assert str(error).endswith("pull request link not written")


def test_r6_a_second_pull_request_is_refused(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        first = link(ledger).event
    error = assert_refused(db, ValidationError, f"loop-pr-link-already-exists:{ITEM}", pr=8)
    assert str(error) == f"loop-pr-link-already-exists:{ITEM}"
    assert pr_links(db) == [first]


@pytest.mark.parametrize(
    ("target", "pr"),
    [
        ("example/repo#7", 7),
        ("https://github.com/Example/Repo/pull/7", 7),
        ("#7", 7),
        ("example/repo#8", 7),
    ],
    ids=["short-form-same-pr", "other-case-same-pr", "bare-number", "short-form-other-pr"],
)
def test_r6_any_existing_pr_link_refuses_whatever_its_form_or_key(
    tmp_path: Path, target: str, pr: int
) -> None:
    # Only a restored chain can hold a pr link on a managed item that this
    # writer did not write, under a key that is not the writer's.
    export_dir = tmp_path / "chain"
    write_chain_export(
        export_dir,
        [
            (ITEM, "item.created", {"type": "ticket", "title": "Restored loop item"}),
            (ITEM, "loop.managed", {}),
            (ITEM, "item.state", {"status": "claimed", "claim": HOLDER}),
            (ITEM, "item.linked", {"rel": "pr", "target": target}),
        ],
    )
    dest = tmp_path / "restored.db"
    restore_export(export_dir, dest, system="home", accept_loop_events=True)
    error = assert_refused(str(dest), ValidationError, f"loop-pr-link-already-exists:{ITEM}", pr=pr)
    assert str(error) == f"loop-pr-link-already-exists:{ITEM}"


@pytest.mark.parametrize(
    "target",
    [
        URL,
        "Example/Repo#7",
        "example/repo#7",
        "https://github.com/EXAMPLE/repo/pull/7",
    ],
    ids=["url", "short-other-case", "short", "url-other-case"],
)
def test_r7_a_pull_request_another_item_links_is_refused(db: str, target: str) -> None:
    with open_ledger(db, create=False) as ledger:
        ledger.append(
            kind="item.linked",
            payload={"rel": "pr", "target": target},
            item_id=OTHER,
            idempotency_key=f"generic:{OTHER}",
        )
    error = assert_refused(db, ValidationError, f"loop-pr-linked-elsewhere:{OTHER}")
    assert str(error) == f"loop-pr-linked-elsewhere:{OTHER}"
    assert pr_links(db) == []


@pytest.mark.parametrize(
    ("rel", "target"),
    [
        ("pr", "#7"),
        ("pr", "https://github.com/example/repo/pull/7/files"),
        ("pr", "https://github.com/example/repo/pull/07"),
        ("pr", "http://github.com/example/repo/pull/7"),
        ("pr", "example/repo#8"),
        ("pr", "other/repo#7"),
        ("url", URL),
    ],
    ids=["bare-number", "files", "leading-zero", "http", "other-number", "other-repo",
         "other-rel"],
)
def test_r7_a_link_the_reader_does_not_match_does_not_refuse(
    db: str, rel: str, target: str
) -> None:
    with open_ledger(db, create=False) as ledger:
        ledger.append(
            kind="item.linked",
            payload={"rel": rel, "target": target},
            item_id=OTHER,
            idempotency_key=f"generic:{OTHER}",
        )
        assert link(ledger).created is True
        assert receipts.pr_item(ledger, URL) == ITEM


@pytest.mark.parametrize("owner", [ITEM, OTHER])
def test_r8_another_event_under_the_links_key_is_a_conflict(db: str, owner: str) -> None:
    with open_ledger(db, create=False) as ledger:
        squat = ledger.append(
            kind="item.note", payload={"text": "squat"}, item_id=owner, idempotency_key=KEY
        )
    assert_refused(db, IdempotencyConflict, "idempotency_key")
    with open_ledger(db, create=False) as ledger:
        assert ledger.event_by_key(KEY) == squat
    assert pr_links(db) == []


@pytest.mark.parametrize("capability", [None, object(), "loop"], ids=["none", "object", "string"])
def test_r10_the_door_needs_the_loop_writer_capability(db: str, capability) -> None:
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(ValidationError, match="^loop-writer-capability-required$"):
            ledger._append_loop_pr_link(
                capability,
                kind="item.linked",
                payload={"rel": "pr", "target": URL},
                item_id=ITEM,
                idempotency_key=KEY,
            )
    assert all_events(db) == before


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("item.note", {"text": "a note"}),
        ("item.linked", {"rel": "branch", "target": "feat/x"}),
        ("item.state", {"status": "closed"}),
    ],
    ids=["note", "linked-branch", "state-closed"],
)
def test_r11_the_door_takes_only_a_pr_link(db: str, kind: str, payload: dict) -> None:
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(ValidationError, match="^loop-pr-link-required$"):
            ledger._append_loop_pr_link(
                _LOOP_WRITER_CAPABILITY,
                kind=kind,
                payload=payload,
                item_id=ITEM,
                idempotency_key=f"door:{kind}",
            )
    assert all_events(db) == before
