"""Merge write-ahead on a PR-linked item, without installing a work loop."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep

import pytest

from ledger import receipts
from ledger.store import open_ledger
from ledger.drill import restore_drill
from ledger.export import export_events, verify_export
from ledger.loop import _LOOP_WRITER_CAPABILITY, claim_operation, manage_item
from ledger.restore import replay_events, restore_export
from ledger.schema import ValidationError, validate_payload
from ledger.store import LedgerError
from ledger.verbs import CliError

from receipt_helpers import link, new_item

ITEM = "parent-item"
HEAD = "a" * 40
PR = "https://github.com/example/repo/pull/12"
KEY = f"merge:example/repo#12:{HEAD}"
PAYLOAD = {"key": KEY, "pr": PR, "head": HEAD}
INTENT = "review.merge-intent"
DONE = "review.merge-done"


def invoke(lg, kind=INTENT, payload=None, item=ITEM, **stamps):
    writer = receipts.write_merge_intent if kind == INTENT else receipts.write_merge_done
    try:
        return writer(lg, item, PAYLOAD if payload is None else payload, **stamps)
    except Exception as exc:
        return exc


def write(lg, kind=INTENT, payload=None, item=ITEM, **stamps):
    result = invoke(lg, kind, payload, item, **stamps)
    assert not isinstance(result, Exception), str(result)
    return result


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.db"
    with open_ledger(path) as lg:
        new_item(lg, ITEM)
        link(lg, ITEM, PR)
    return path


def test_unmanaged_lifecycle_and_retries_survive_reopen(db):
    with open_ledger(db) as lg:
        first = write(lg, actor="writer-a")
        assert (first.state, first.created, first.event.kind) == ("started", True, INTENT)
        assert first.event.payload == PAYLOAD
        again = write(lg, actor="writer-b")
        assert (again.state, again.created, again.event) == ("started", False, first.event)
        assert lg.events(kind=INTENT) == [first.event]
        assert lg.events(kind="loop.managed") == []
    with open_ledger(db) as lg:
        done = write(lg, DONE)
        assert (done.state, done.created, done.event.kind) == ("finished", True, DONE)
        assert done.event.payload == PAYLOAD
    with open_ledger(db) as lg:
        before = lg.events()
        for kind in (INTENT, DONE):
            again = write(lg, kind)
            assert (again.state, again.created, again.event) == ("finished", False, done.event)
        assert lg.events() == before
        assert len(lg.events(kind=INTENT)) == len(lg.events(kind=DONE)) == 1
        lg.verify()


def test_done_requires_intent_for_exact_item_and_key(db):
    with open_ledger(db) as lg:
        before = lg.events()
        error = invoke(lg, DONE)
        assert isinstance(error, CliError), error
        assert (error.code, error.exit_code) == ("merge_intent_missing", 3)
        assert lg.events() == before
        write(lg)
        other = {**PAYLOAD, "key": f"merge:example/repo#12:{'b' * 40}", "head": "b" * 40}
        before = lg.events()
        error = invoke(lg, DONE, other)
        assert isinstance(error, CliError), error
        assert error.code == "merge_intent_missing"
        assert lg.events() == before
        write(lg, payload=other)
        write(lg, DONE, other)
        assert write(lg).state == "started"


@pytest.mark.parametrize("kind", [INTENT, DONE])
@pytest.mark.parametrize("field,value,reason", [
    ("extra", True, "unknown-key:extra"),
    ("head", "a" * 7, "bad-type:head"),
    ("head", "A" * 40, "bad-type:head"),
    ("head", 123, "bad-type:head"),
    ("head", "b" * 40, "merge-key-mismatch"),
    ("pr", "https://github.com/example/other/pull/12", "merge-key-mismatch"),
    ("pr", "https://github.com/example/repo/pull/13", "merge-key-mismatch"),
    ("pr", "#12", "bad-type:pr"),
    ("key", f"merge:example/other#12:{HEAD}", "merge-key-mismatch"),
    ("key", f"merge:example/repo#13:{HEAD}", "merge-key-mismatch"),
    ("key", f"merge:example/repo#12:{'b' * 40}", "merge-key-mismatch"),
    ("key", f"Merge:example/repo#12:{HEAD}", "bad-type:key"),
    ("key", KEY + " ", "bad-type:key"),
    ("key", "- " + KEY, "bad-type:key"),
    ("key", "```" + KEY + "```", "bad-type:key"),
    ("key", f"merge:example/repo#12:{HEAD[:7]}", "bad-type:key"),
    ("key", f"merge:example/repo#12:{HEAD.upper()}", "bad-type:key"),
    ("key", f"merge:example/repo#0:{HEAD}", "bad-type:key"),
    ("key", f"merge:Example/repo#12:{HEAD}", "bad-type:key"),
    ("key", None, "bad-type:key"),
])
def test_payload_grammar_refuses_without_writing(db, kind, field, value, reason):
    with open_ledger(db) as lg:
        before = lg.events()
        result = invoke(lg, kind, {**PAYLOAD, field: value})
        assert isinstance(result, ValidationError), result
        assert result.reason == reason
        assert lg.events() == before


@pytest.mark.parametrize("kind", [INTENT, DONE])
@pytest.mark.parametrize("stamp", ["actor", "session"])
def test_credential_shaped_stamp_is_refused_without_writing(db, kind, stamp):
    with open_ledger(db) as lg:
        if kind == DONE:
            write(lg)
        before = len(lg.events())
        result = invoke(lg, kind, **{stamp: "ghp_" + "A" * 36})
        assert isinstance(result, CliError), result
        assert result.code == "receipt_credential_shape"
        assert len(lg.events()) == before


@pytest.mark.parametrize("kind", [INTENT, DONE])
def test_credential_shaped_item_id_is_refused_without_writing(tmp_path, kind):
    item = "ghp_" + "A" * 36
    with open_ledger(tmp_path / "credentials.db") as lg:
        new_item(lg, item)
        link(lg, item, PR)
        if kind == DONE:
            # Seed an existing intent so done is otherwise eligible to write.
            lg._append_receipt_event(
                receipts._RECEIPT_WRITER_CAPABILITY, kind=INTENT, payload=PAYLOAD,
                item_id=item, idempotency_key="prior-intent",
            )
        before = len(lg.events())
        result = invoke(lg, kind, item=item)
        assert isinstance(result, CliError), result
        assert result.code == "receipt_credential_shape"
        assert len(lg.events()) == before


@pytest.mark.parametrize("kind", [INTENT, DONE])
@pytest.mark.parametrize("field", ["key", "pr", "head"])
def test_required_payload_fields(db, kind, field):
    payload = dict(PAYLOAD)
    del payload[field]
    with open_ledger(db) as lg:
        before = lg.events()
        result = invoke(lg, kind, payload)
        assert isinstance(result, ValidationError), result
        assert result.reason == f"missing-key:{field}"
        assert lg.events() == before


@pytest.mark.parametrize("target", [
    PR, "example/repo#12", "EXAMPLE/Repo#12",
    "https://github.com/EXAMPLE/Repo/pull/12",
])
def test_link_resolution_and_multiple_pr_links(tmp_path, target):
    with open_ledger(tmp_path / "links.db") as lg:
        new_item(lg, ITEM)
        link(lg, ITEM, "example/other#12")
        link(lg, ITEM, target)
        assert write(lg).state == "started"
        assert write(lg, DONE).state == "finished"


def test_mixed_case_intent_and_lowercase_done_share_key(db):
    with open_ledger(db) as lg:
        intent = write(lg, payload={**PAYLOAD, "pr": "https://github.com/Example/Repo/pull/12"})
        assert intent.state == "started"
        done = write(lg, DONE)
        assert done.state == "finished"
        assert intent.event.payload["key"] == done.event.payload["key"] == KEY
        assert lg.events(kind=INTENT) == [intent.event]
        assert lg.events(kind=DONE) == [done.event]


@pytest.mark.parametrize("stage", ["absent", "started", "finished"])
@pytest.mark.parametrize("kind", [INTENT, DONE])
def test_live_pr_resolution_refuses_ambiguous_item_even_on_retry(db, stage, kind):
    with open_ledger(db) as lg:
        if stage != "absent":
            write(lg)
        if stage == "finished":
            write(lg, DONE)
        new_item(lg, "other-item")
        link(lg, "other-item", "example/repo#12")
        before = lg.events()
        result = invoke(lg, kind)
        assert isinstance(result, CliError), result
        assert (result.code, result.exit_code) == ("pr_item_ambiguous", 3)
        assert lg.events() == before


@pytest.mark.parametrize("target,rel,code", [
    ("example/other#12", "pr", "pr_item_missing"),
    ("example/repo#13", "pr", "pr_item_missing"),
    ("#12", "pr", "pr_item_missing"),
    (PR, "url", "pr_item_missing"),
    (None, "pr", "pr_item_missing"),
    (PR, "other-item", "receipt_item_mismatch"),
])
@pytest.mark.parametrize("kind", [INTENT, DONE])
def test_item_binding_refusal_writes_nothing(tmp_path, target, rel, code, kind):
    with open_ledger(tmp_path / "binding.db") as lg:
        new_item(lg, ITEM)
        if rel == "other-item":
            new_item(lg, "other-item")
            link(lg, "other-item", target)
        elif target is not None:
            link(lg, ITEM, target, rel)
        before = lg.events()
        result = invoke(lg, kind)
        assert isinstance(result, CliError), result
        assert (result.code, result.exit_code) == (code, 3)
        assert lg.events() == before


def test_loop_operation_path_is_unchanged(tmp_path):
    with open_ledger(tmp_path / "loop.db") as lg:
        new_item(lg, ITEM)
        manage_item(lg, ITEM, idempotency_key="manage")
        claim = claim_operation(lg, ITEM, "merge", KEY)
        assert claim.created and claim.event.kind == "loop.intent"
        assert claim_operation(lg, ITEM, "merge", KEY).created is False
        assert lg.events(kind="loop.intent") == [claim.event]


def test_loop_writer_still_refuses_unmanaged_pr_item(db):
    with open_ledger(db) as lg:
        before = lg.events()
        errors = []
        for call in (
            lambda: manage_item(lg, ITEM, idempotency_key="manage"),
            lambda: claim_operation(lg, ITEM, "merge", KEY),
        ):
            try:
                call()
            except ValidationError as exc:
                errors.append(exc.reason)
        assert errors == [f"loop-pr-link-already-exists:{ITEM}", f"loop-item-not-managed:{ITEM}"]
        assert lg.events() == before


@pytest.mark.parametrize("kind", [INTENT, DONE])
def test_append_failure_rolls_back_and_rerun_is_safe(db, monkeypatch, kind):
    with open_ledger(db) as lg:
        if kind == DONE:
            write(lg)
        before = lg.events()
        append = lg._append_receipt_event

        def stop(*args, **kwargs):
            append(*args, **kwargs)
            raise RuntimeError("stopped after insert")

        with monkeypatch.context() as patch:
            patch.setattr(lg, "_append_receipt_event", stop)
            error = invoke(lg, kind)
            assert isinstance(error, RuntimeError), error
            assert str(error) == "stopped after insert"
        assert lg.events() == before
        result = write(lg, kind)
        assert result.created
        assert len(lg.events(kind=kind)) == 1


@pytest.mark.parametrize("kind", [INTENT, DONE])
def test_concurrent_writers_append_once(db, kind):
    if kind == DONE:
        with open_ledger(db) as lg:
            write(lg)
    start = Barrier(2)

    def worker(actor):
        with open_ledger(db) as lg:
            append = lg._append_receipt_event

            def slow_append(*args, **kwargs):
                sleep(0.05)
                return append(*args, **kwargs)

            lg._append_receipt_event = slow_append
            start.wait(timeout=10)
            return invoke(lg, kind, actor=actor)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["writer-a", "writer-b"]))
    assert all(not isinstance(result, Exception) for result in results), results
    assert sorted(result.created for result in results) == [False, True]
    assert results[0].event == results[1].event
    with open_ledger(db) as lg:
        assert lg.events(kind=kind) == [results[0].event]
        lg.verify()


@pytest.mark.parametrize("finished", [False, True])
def test_restore_reservation_digest_replay_and_drill(db, tmp_path, finished):
    with open_ledger(db) as lg:
        write(lg)
        if finished:
            write(lg, DONE)
        source = lg.events()
        export_dir = tmp_path / "export"
        export_events(lg, export_dir)
    dest = tmp_path / "restore.db"
    try:
        restore_export(export_dir, dest, system="example")
    except LedgerError as exc:
        error = str(exc)
    else:
        error = ""
    assert "pass --accept-loop-events" in error
    assert not dest.exists()
    seen = []
    restore_export(export_dir, dest, system="example", accept_loop_events=True,
                   on_accept_loop_events=lambda n, kinds: seen.append((n, kinds)))
    expected_kinds = (DONE, INTENT) if finished else (INTENT,)
    assert seen == [(2 if finished else 1, expected_kinds)]
    with open_ledger(dest) as lg:
        lg.verify()
        assert lg.events()[:len(source)] == source
        assert write(lg).state == ("finished" if finished else "started")
    with open_ledger(tmp_path / "replay.db") as lg:
        try:
            replay_events(lg, verify_export(export_dir))
        except LedgerError as exc:
            error = str(exc)
        else:
            error = ""
        assert error == f"replay refuses reserved event: {INTENT}"
        assert lg.events() == []
    try:
        result = restore_drill(source, export_dir, tmp_path / "drill.db", system=None)
    except (LedgerError, RuntimeError) as exc:
        result = exc
    assert not isinstance(result, Exception), str(result)
    assert result.events == len(source)


@pytest.mark.parametrize("kind", [INTENT, DONE])
def test_restore_schema_checks_cross_fields(kind):
    try:
        validate_payload(kind, {**PAYLOAD, "head": "b" * 40})
    except ValidationError as exc:
        error = exc.reason
    else:
        error = ""
    assert error == "merge-key-mismatch"


@pytest.mark.parametrize("kind", [INTENT, DONE])
def test_receipt_capability_is_required_for_merge_records(db, kind):
    with open_ledger(db) as lg:
        before = lg.events()
        try:
            result = lg._append_receipt_event(
                _LOOP_WRITER_CAPABILITY, kind=kind, payload=PAYLOAD,
                item_id=ITEM, idempotency_key="forged",
            )
        except ValidationError as exc:
            result = exc.reason
        assert result == "receipt-writer-capability-required"
        assert lg.events() == before


def test_restore_rejects_altered_stream_digest(db, tmp_path):
    import json

    with open_ledger(db) as lg:
        write(lg)
        write(lg, DONE)
        export_dir = tmp_path / "export"
        export_events(lg, export_dir)
    digest = export_dir / "stream-digest.json"
    digest.write_text(json.dumps({"algorithm": "sha256", "digest": "0" * 64, "events": 4}))
    dest = tmp_path / "restore.db"
    try:
        restore_export(export_dir, dest, system="example", accept_loop_events=True)
    except LedgerError as exc:
        result = str(exc)
    else:
        result = ""
    assert "stream digest" in result
    assert not dest.exists()
