"""The hand-off: a ``loop.intent`` of payload version 2.

Only ``claim_operation`` with ``hand_off=True`` writes one, on a ``build``
operation of a loop-managed item, as the holder of the item's live claim,
naming one controller and all three seams, while no other hand-off on the
item is open. Every refusal here also checks that nothing was written.
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

from ledger import lease
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
    claim_operation,
    close_item,
    finish_operation,
    manage_item,
    open_hand_off,
    take_item,
)
from ledger.restore import restore_export
from ledger.schema import ValidationError, validate_item_id, validate_payload
from ledger.store import (
    GENESIS_HASH,
    Event,
    IdempotencyConflict,
    LedgerError,
    canonical_dumps,
    claim_database,
    event_digest,
    open_ledger,
)

ITEM = "item-1"
HOLDER = "loop-1"
CONTROLLER = "director"
SEAMS = {"build": "omp-builder", "review": "review-gate", "outward": "draft-pr"}
OMIT = object()


def invoke(monkeypatch, db: str, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--db", db, *args])
    return code, out.getvalue(), err.getvalue()


def new_item(monkeypatch, db: str, item_id: str = ITEM) -> None:
    code, _out, err = invoke(
        monkeypatch, db, "new", "--id", item_id, "--type", "ticket", "--title", "Loop item"
    )
    assert code == 0, err


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    """A loop-managed item whose live (stamped) claim names HOLDER."""
    path = str(tmp_path / "ledger.db")
    new_item(monkeypatch, path)
    with open_ledger(path) as ledger:
        take_item(
            ledger, ITEM, HOLDER, idempotency_key=f"manage:{ITEM}", claim_key=f"take:{ITEM}:1"
        )
    return path


def all_events(db: str) -> list[Event]:
    with open_ledger(db, create=False) as ledger:
        return ledger.events()


def hand_off(ledger, operation_id: str = "build-1", **overrides):
    """``claim_operation`` with a full hand-off; an override of OMIT drops that argument."""
    kwargs = {
        "operation": "build",
        "holder": HOLDER,
        "hand_off": True,
        "controller": CONTROLLER,
        "seams": dict(SEAMS),
        "item_id": ITEM,
    }
    kwargs.update(overrides)
    kwargs = {key: value for key, value in kwargs.items() if value is not OMIT}
    item_id = kwargs.pop("item_id")
    operation = kwargs.pop("operation")
    return claim_operation(ledger, item_id, operation, operation_id, **kwargs)


def assert_refused(db: str, error: type[BaseException], name: str, **overrides) -> BaseException:
    """The hand-off is refused with ``name`` as the message's start, and writes nothing."""
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(error, match=f"^{re.escape(name)}") as caught:
            hand_off(ledger, **overrides)
    assert all_events(db) == before, "a refused hand-off must write nothing"
    return caught.value


def full_payload(**extra) -> dict:
    return {
        "operation": "build",
        "operation_id": "build-1",
        "controller": CONTROLLER,
        "seams": dict(SEAMS),
        **extra,
    }


def write_chain_export(export_dir: Path, events: list[tuple[str, int, dict]]) -> None:
    """Write a valid hash chain of ``(kind, payload_version, payload)`` on ITEM."""
    export_dir.mkdir()
    previous = GENESIS_HASH
    for event_id, (kind, version, payload) in enumerate(events, 1):
        event = Event(
            id=event_id,
            kind=kind,
            payload_version=version,
            payload=payload,
            item_id=ITEM,
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


# --- F: the feature --------------------------------------------------------


def test_f1_a_hand_off_payload_validates_as_loop_intent_version_2() -> None:
    payload = full_payload(target="org/repo#1", head="abc123", asked_event_id=3, source="row")
    assert validate_payload("loop.intent", dict(payload), 2) == payload
    assert validate_item_id("loop.intent", ITEM, 2) == ITEM
    # The controller and each seam may be up to 64 characters.
    long_name = "a" * 64
    edge = full_payload(controller=long_name, seams={**SEAMS, "outward": long_name})
    assert validate_payload("loop.intent", dict(edge), 2) == edge


def test_f2_hand_off_writes_one_version_2_intent_and_the_unstamped_claim(db: str) -> None:
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        result = hand_off(ledger)
    assert result.created is True
    after = all_events(db)
    written = after[len(before):]
    assert [event.kind for event in written] == ["loop.intent", "item.state"]
    intent, claim = written
    assert intent == result.event
    assert intent.payload_version == 2
    assert intent.payload == full_payload()
    assert intent.idempotency_key == f"loop:{ITEM}:build:build-1:intent"
    # The holder's claim, rewritten unstamped in the same transaction.
    assert claim.id == intent.id + 1
    assert claim.payload == {"status": "claimed", "claim": HOLDER}
    assert claim.idempotency_key == f"loop:{ITEM}:build:build-1:hand-off"


def test_f2_the_stored_seams_do_not_alias_the_callers_dict(db: str) -> None:
    seams = dict(SEAMS)
    with open_ledger(db, create=False) as ledger:
        result = hand_off(ledger, seams=seams)
    seams["build"] = "changed-after"
    assert result.event.payload["seams"] == SEAMS
    with open_ledger(db, create=False) as ledger:
        assert open_hand_off(ledger, ITEM).payload["seams"] == SEAMS


def test_f3_reader_returns_the_hand_off_and_nothing_after_finish(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        assert open_hand_off(ledger, ITEM) is None
        written = hand_off(ledger).event
        found = open_hand_off(ledger, ITEM)
        assert found == written
        assert found.payload["controller"] == CONTROLLER
        assert found.payload["seams"] == SEAMS
        assert lease.current_lease(ledger._conn, ITEM)["expires_at"] is None

        finish_operation(ledger, ITEM, "build", "build-1", holder=HOLDER, verdict="pass")
        assert open_hand_off(ledger, ITEM) is None
        # The claim is stamped again once the build's result is recorded.
        stamped = lease.current_lease(ledger._conn, ITEM)
        assert stamped["expires_at"] is not None
        assert not lease.is_expired(stamped["expires_at"])


def test_f4_export_verify_and_restore_keep_version_2(
    db: str, tmp_path: Path, monkeypatch
) -> None:
    claim_database(db, "home")
    with open_ledger(db, create=False) as ledger:
        written = hand_off(ledger).event

    def exported_intent() -> dict:
        code, out, err = invoke(monkeypatch, db, "--json", "export-item", ITEM)
        assert code == 0, err
        records = json.loads(out)["data"]["events"]
        intents = [record for record in records if record["kind"] == "loop.intent"]
        assert len(intents) == 1
        return intents[0]

    record = exported_intent()
    assert record["payload_version"] == 2
    assert record["payload"] == full_payload()
    code, _out, err = invoke(monkeypatch, db, "verify")
    assert code == 0, err

    with open_ledger(db, create=False) as ledger:
        finish_operation(ledger, ITEM, "build", "build-1", holder=HOLDER)
    record = exported_intent()
    assert record["payload_version"] == 2
    assert record["payload"] == full_payload()
    code, _out, err = invoke(monkeypatch, db, "verify")
    assert code == 0, err

    export_dir = tmp_path / "export"
    with open_ledger(db, create=False) as ledger:
        export_events(ledger, export_dir)
        original = ledger.events()
    exported = [event for event in verify_export(export_dir) if event.kind == "loop.intent"]
    assert [(event.id, event.payload_version, event.payload) for event in exported] == [
        (written.id, 2, full_payload())
    ]
    dest = tmp_path / "restored.db"
    restore_export(export_dir, dest, system="home", accept_loop_events=True)
    with open_ledger(dest, create=False) as restored:
        restored.verify(system="home")
        assert restored.events()[: len(original)] == original
        assert open_hand_off(restored, ITEM) is None


def test_f5_an_exact_retry_returns_the_stored_hand_off_and_writes_nothing(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        first = hand_off(ledger)
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        again = hand_off(ledger)
    assert again.created is False
    assert again.event == first.event
    assert all_events(db) == before, "a retry must write no second intent or claim"


# --- G: what stays as it is ------------------------------------------------


@pytest.mark.parametrize("flag", [False, True], ids=["plain", "hand_off"])
@pytest.mark.parametrize("operation", ["build", "review"])
def test_g1_without_the_fields_the_intent_is_version_1_as_before(
    db: str, operation: str, flag: bool
) -> None:
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        result = claim_operation(
            ledger, ITEM, operation, "op-1", holder=HOLDER, hand_off=flag, target="org/repo#1"
        )
        assert open_hand_off(ledger, ITEM) is None
    assert result.created is True
    assert result.event.payload_version == 1
    assert result.event.payload == {
        "operation": operation,
        "operation_id": "op-1",
        "target": "org/repo#1",
    }
    written = all_events(db)[len(before):]
    expected = ["loop.intent", "item.state"] if flag else ["loop.intent"]
    assert [event.kind for event in written] == expected


def test_g2_generic_writers_cannot_write_a_hand_off(db: str, monkeypatch) -> None:
    before = all_events(db)
    payload = full_payload()
    code, _out, err = invoke(
        monkeypatch,
        db,
        "append",
        "--kind",
        "loop.intent",
        "--item",
        ITEM,
        "--payload-version",
        "2",
        "--payload",
        json.dumps(payload),
    )
    assert code == 3, err
    assert "reserved-kind:loop.intent" in err
    batch = [{"kind": "loop.intent", "payload_version": 2, "payload": payload}]
    code, _out, err = invoke(monkeypatch, db, "append-many", ITEM, stdin=json.dumps(batch))
    assert code == 3, err
    assert "reserved-kind:loop.intent" in err
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(ValidationError, match="^reserved-kind:loop.intent$"):
            ledger.append(
                kind="loop.intent",
                payload=payload,
                payload_version=2,
                item_id=ITEM,
                idempotency_key="generic-hand-off",
            )
        with pytest.raises(ValidationError, match="^reserved-kind:loop.intent$"):
            ledger.append_many(
                [
                    {
                        "kind": "loop.intent",
                        "payload": payload,
                        "payload_version": 2,
                        "item_id": ITEM,
                        "idempotency_key": "generic-hand-off-batch",
                    }
                ]
            )
    assert all_events(db) == before


def test_g3_a_version_1_build_intent_is_never_a_hand_off(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        claim_operation(ledger, ITEM, "build", "plain-1", holder=HOLDER)
        assert open_hand_off(ledger, ITEM) is None
        handed = hand_off(ledger, "build-1")
        assert handed.created is True
        assert open_hand_off(ledger, ITEM) == handed.event
        # A version 1 build intent, and a version 1 session step, beside the
        # open hand-off are allowed and leave the reader as it was.
        later = claim_operation(ledger, ITEM, "build", "plain-2", holder=HOLDER)
        session_step = claim_operation(ledger, ITEM, "build", "plain-3", holder=HOLDER, hand_off=True)
        assert later.created and session_step.created
        assert later.event.payload_version == session_step.event.payload_version == 1
        assert open_hand_off(ledger, ITEM) == handed.event


# --- R: every refusal by name, each writing nothing --------------------------


@pytest.mark.parametrize(
    ("overrides", "name"),
    [
        ({"seams": OMIT}, "missing-key:seams"),
        ({"controller": OMIT}, "missing-key:controller"),
    ],
    ids=["controller-only", "seams-only"],
)
def test_r1_one_hand_off_field_without_the_other(db: str, overrides: dict, name: str) -> None:
    assert_refused(db, ValidationError, name, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"hand_off": False},
        {"hand_off": False, "seams": OMIT},
        {"hand_off": False, "controller": OMIT},
        {"hand_off": False, "holder": OMIT},
    ],
    ids=["both", "controller", "seams", "no-holder"],
)
def test_r2_hand_off_fields_need_hand_off(db: str, overrides: dict) -> None:
    assert_refused(db, ValidationError, "hand-off-fields-need-hand-off", **overrides)


@pytest.mark.parametrize("operation", ["review", "merge", "decision", "ping"])
def test_r3_hand_off_fields_on_another_operation(db: str, operation: str) -> None:
    assert_refused(db, ValidationError, "bad-type:operation", operation=operation)


@pytest.mark.parametrize(
    "controller",
    [
        "",
        " director",
        "dir ector",
        "director\n",
        "dir\nector",
        "dirеctor",  # CYRILLIC SMALL LETTER IE, a look-alike of "e"
        "​director",  # ZERO WIDTH SPACE
        "-director",
        "director/seat",
        7,
    ],
    ids=["empty", "leading-space", "space", "trailing-newline", "line-break", "cyrillic",
         "zero-width", "leading-hyphen", "slash", "not-a-string"],
)
def test_r4_controller_must_be_one_ascii_token(db: str, controller) -> None:
    error = assert_refused(db, ValidationError, "bad-type:controller", controller=controller)
    assert str(error) == "bad-type:controller"


def test_r5_controller_over_64_characters(db: str) -> None:
    assert_refused(db, ValidationError, "too-long:controller", controller="a" * 65)


@pytest.mark.parametrize(
    "seams",
    [["omp-builder", "review-gate", "draft-pr"], "omp-builder", 3, (("build", "x"),)],
    ids=["list", "string", "integer", "pairs"],
)
def test_r6_seams_must_be_an_object(db: str, seams) -> None:
    error = assert_refused(db, ValidationError, "bad-type:seams", seams=seams)
    assert str(error) == "bad-type:seams"


@pytest.mark.parametrize("seam", ["build", "review", "outward"])
def test_r7_a_seam_missing(db: str, seam: str) -> None:
    partial = {key: value for key, value in SEAMS.items() if key != seam}
    error = assert_refused(db, ValidationError, f"missing-key:seams.{seam}", seams=partial)
    assert str(error) == f"missing-key:seams.{seam}"


@pytest.mark.parametrize(
    ("seams", "name"),
    [
        ({**SEAMS, "deploy": "x"}, "unknown-key:seams.deploy"),
        ({"Build": "x", "review": "y", "outward": "z"}, "unknown-key:seams.Build"),
        ({}, "missing-key:seams.build"),
    ],
    ids=["extra", "other-case", "empty"],
)
def test_r8_a_seam_the_ledger_does_not_know(db: str, seams: dict, name: str) -> None:
    error = assert_refused(db, ValidationError, name, seams=seams)
    assert str(error) == name


@pytest.mark.parametrize("seam", ["build", "review", "outward"])
@pytest.mark.parametrize(
    "value",
    ["", "omp builder", "omp-builder\n", "é", "-x", None, 5],
    ids=["empty", "space", "newline", "non-ascii", "leading-hyphen", "null", "integer"],
)
def test_r9_an_implementation_that_is_not_a_token(db: str, seam: str, value) -> None:
    error = assert_refused(
        db, ValidationError, f"bad-type:seams.{seam}", seams={**SEAMS, seam: value}
    )
    assert str(error) == f"bad-type:seams.{seam}"


def test_r9_an_implementation_over_64_characters(db: str) -> None:
    assert_refused(db, ValidationError, "too-long:seams.review", seams={**SEAMS, "review": "r" * 65})


@pytest.mark.parametrize("key", ["parent", "note", "Controller"])
def test_r10_any_other_key_on_version_2(key: str) -> None:
    with pytest.raises(ValidationError, match=f"^unknown-key:{key}$"):
        validate_payload("loop.intent", full_payload(**{key: "x"}), 2)


@pytest.mark.parametrize(
    ("extra", "name"),
    [
        ({"controller": CONTROLLER}, "unknown-key:controller"),
        ({"seams": dict(SEAMS)}, "unknown-key:seams"),
        ({"controller": CONTROLLER, "seams": dict(SEAMS)}, "unknown-key:controller"),
    ],
    ids=["controller", "seams", "both"],
)
def test_r11_version_1_takes_neither_field(extra: dict, name: str) -> None:
    with pytest.raises(ValidationError, match=f"^{name}$"):
        validate_payload("loop.intent", {"operation": "build", "operation_id": "b-1", **extra}, 1)


def test_r12_an_item_that_is_not_loop_managed(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "ledger.db")
    new_item(monkeypatch, db, "item-2")
    with open_ledger(db, create=False) as ledger:
        # HOLDER's own live claim, so only the missing loop.managed refuses.
        ledger.append(
            kind="item.state",
            payload={"status": "claimed", "claim": HOLDER, "expires_at": lease.expires_at(3600)},
            item_id="item-2",
            idempotency_key="claim:item-2",
        )
    assert_refused(db, ValidationError, "loop-item-not-managed:item-2", item_id="item-2")


def _no_claim(tmp_path: Path, monkeypatch) -> str:
    db = str(tmp_path / "unclaimed.db")
    new_item(monkeypatch, db)
    with open_ledger(db, create=False) as ledger:
        manage_item(ledger, ITEM, idempotency_key=f"manage:{ITEM}")
    return db


def _other_holder(tmp_path: Path, monkeypatch) -> str:
    db = str(tmp_path / "other.db")
    new_item(monkeypatch, db)
    with open_ledger(db, create=False) as ledger:
        take_item(
            ledger, ITEM, "other-seat", idempotency_key=f"manage:{ITEM}", claim_key="take:other"
        )
    return db


def _expired(tmp_path: Path, monkeypatch) -> str:
    db = str(tmp_path / "expired.db")
    new_item(monkeypatch, db)
    with open_ledger(db, create=False) as ledger:
        take_item(ledger, ITEM, HOLDER, idempotency_key=f"manage:{ITEM}", claim_key="take:1")
        ledger.append(
            kind="item.state",
            payload={
                "status": "claimed",
                "claim": HOLDER,
                "expires_at": lease.stamp(lease.now() - timedelta(hours=1)),
            },
            item_id=ITEM,
            idempotency_key="claim:expired",
        )
    return db


def _closed(tmp_path: Path, monkeypatch) -> str:
    db = str(tmp_path / "closed.db")
    new_item(monkeypatch, db)
    with open_ledger(db, create=False) as ledger:
        take_item(ledger, ITEM, HOLDER, idempotency_key=f"manage:{ITEM}", claim_key="take:1")
        claim_operation(ledger, ITEM, "merge", "merge-1", holder=HOLDER)
        done = finish_operation(ledger, ITEM, "merge", "merge-1", holder=HOLDER)
        close_item(ledger, ITEM, done.id, idempotency_key=f"loop:{ITEM}:closed", holder=HOLDER)
    return db


@pytest.mark.parametrize(
    "setup",
    [_no_claim, _other_holder, _expired, _closed],
    ids=["no-claim", "other-holder", "expired", "closed"],
)
def test_r13_only_the_holder_of_the_live_claim_hands_off(
    tmp_path: Path, monkeypatch, setup
) -> None:
    db = setup(tmp_path, monkeypatch)
    error = assert_refused(db, lease.ClaimRefused, "")
    assert error.code == "claim_lost"
    assert str(error).endswith("build intent not written")


def test_r14_a_second_open_hand_off_is_refused(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        hand_off(ledger, "build-1")
    assert_refused(db, ValidationError, "loop-hand-off-open:build-1", operation_id="build-2")
    other = {"build": "b2", "review": "r2", "outward": "o2"}
    assert_refused(
        db,
        ValidationError,
        "loop-hand-off-open:build-1",
        operation_id="build-3",
        controller="other-role",
        seams=other,
    )
    # Once that build's loop.done is recorded, the next hand-off is allowed.
    with open_ledger(db, create=False) as ledger:
        finish_operation(ledger, ITEM, "build", "build-1", holder=HOLDER)
        second = hand_off(ledger, "build-2", controller="other-role", seams=other)
        assert second.created is True
        assert open_hand_off(ledger, ITEM) == second.event


@pytest.mark.parametrize(
    "retry",
    [
        {"controller": "other-role"},
        {"seams": {**SEAMS, "outward": "public-default"}},
        {"controller": OMIT, "seams": OMIT},
    ],
    ids=["other-controller", "other-seams", "version-1"],
)
def test_r15_a_changed_retry_under_the_same_operation_id_conflicts(db: str, retry: dict) -> None:
    with open_ledger(db, create=False) as ledger:
        first = hand_off(ledger, "build-1")
    assert_refused(db, IdempotencyConflict, "idempotency_key", operation_id="build-1", **retry)
    with open_ledger(db, create=False) as ledger:
        assert open_hand_off(ledger, ITEM) == first.event


def test_r15_a_hand_off_retried_over_a_version_1_intent_conflicts(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        claim_operation(ledger, ITEM, "build", "build-1", holder=HOLDER, hand_off=True)
    assert_refused(db, IdempotencyConflict, "idempotency_key", operation_id="build-1")


def test_r17_restore_of_a_hand_off_needs_the_accept_flag(db: str, tmp_path: Path) -> None:
    claim_database(db, "home")
    with open_ledger(db, create=False) as ledger:
        hand_off(ledger)
        export_dir = tmp_path / "export"
        export_events(ledger, export_dir)
    dest = tmp_path / "refused.db"
    with pytest.raises(LedgerError, match="--accept-loop-events"):
        restore_export(export_dir, dest, system="home")
    assert not dest.exists(), "a refused restore must leave no database behind"


def test_r18_two_open_hand_offs_are_ambiguous(tmp_path: Path) -> None:
    export_dir = tmp_path / "two-open"
    write_chain_export(
        export_dir,
        [
            ("item.created", 1, {"type": "ticket", "title": "Restored loop item"}),
            ("loop.managed", 1, {}),
            ("item.state", 1, {"status": "claimed", "claim": HOLDER}),
            ("loop.intent", 2, full_payload(operation_id="build-a")),
            ("loop.intent", 2, full_payload(operation_id="build-b")),
        ],
    )
    dest = tmp_path / "restored.db"
    restore_export(export_dir, dest, system="home", accept_loop_events=True)
    with open_ledger(dest, create=False) as ledger:
        with pytest.raises(ValidationError, match=f"^loop-hand-off-ambiguous:{ITEM}$"):
            open_hand_off(ledger, ITEM)
    assert_refused(
        str(dest), ValidationError, f"loop-hand-off-ambiguous:{ITEM}", operation_id="build-c"
    )


# --- Concurrent writers ------------------------------------------------------

_LIB = Path(__file__).resolve().parents[2]
RACER = r"""
import json, sys, time
from pathlib import Path
from ledger import open_ledger
from ledger.loop import claim_operation
from ledger.schema import ValidationError

db, operation_id, go = sys.argv[1], sys.argv[2], Path(sys.argv[3])
seams = json.loads(sys.argv[4])
deadline = time.monotonic() + 30
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.001)
with open_ledger(db, create=False) as ledger:
    try:
        result = claim_operation(
            ledger, "item-1", "build", operation_id,
            holder="loop-1", hand_off=True, controller="director", seams=seams,
        )
        print("created" if result.created else "retried")
    except ValidationError as exc:
        print("refused " + str(exc))
"""


def race(tmp_path: Path, db: str, operation_ids: list[str]) -> list[str]:
    """Start one writer process per operation id, release them together, and
    return each one's outcome line, in the order given."""
    script = tmp_path / "racer.py"
    script.write_text(RACER, encoding="utf-8")
    go = tmp_path / "go"
    env = dict(os.environ, PYTHONPATH=str(_LIB) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), db, operation_id, str(go), json.dumps(SEAMS)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for operation_id in operation_ids
    ]
    go.write_text("go\n", encoding="utf-8")
    outcomes = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
        outcomes.append(stdout.strip())
    return outcomes


def hand_off_events(db: str) -> list[Event]:
    return [e for e in all_events(db) if e.kind == "loop.intent" and e.payload_version == 2]


def test_i3_two_writers_racing_different_hand_offs_leave_one(db: str, tmp_path: Path) -> None:
    outcomes = race(tmp_path, db, ["build-a", "build-b"])
    assert sorted(outcome.split(" ")[0] for outcome in outcomes) == ["created", "refused"]
    winner = ["build-a", "build-b"][outcomes.index("created")]
    loser = outcomes[1 - outcomes.index("created")]
    assert loser == f"refused loop-hand-off-open:{winner}"
    written = hand_off_events(db)
    assert [event.payload["operation_id"] for event in written] == [winner]
    with open_ledger(db, create=False) as ledger:
        assert open_hand_off(ledger, ITEM) == written[0]


def test_i3_two_writers_racing_one_hand_off_write_it_once(db: str, tmp_path: Path) -> None:
    before = len(all_events(db))
    outcomes = race(tmp_path, db, ["build-1", "build-1"])
    assert sorted(outcomes) == ["created", "retried"]
    written = all_events(db)[before:]
    assert [event.kind for event in written] == ["loop.intent", "item.state"]
    assert written[0].payload == full_payload()
