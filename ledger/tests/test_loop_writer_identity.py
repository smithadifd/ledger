"""Generic CLI writes cannot impersonate the internal item-loop writer."""
from __future__ import annotations

import io
import json
import random
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.export import (
    STREAM_DIGEST_NAME,
    event_filename,
    event_record,
    export_events,
    hash_body,
    verify_export,
    write_stream_digest,
)
from ledger.loop import (
    OperationState,
    close_item,
    finish_operation,
    manage_item,
    operation_state,
    start_operation,
)
from ledger.schema import ValidationError
from ledger.verbs import discover_verbs
from ledger.restore import replay_events, restore_export
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


def invoke(monkeypatch, db: str, *args: str) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "foreign-writer")
    monkeypatch.setenv("LEDGER_AGENT", "item-loop")
    monkeypatch.setenv("LEDGER_MODEL", "forged-model")
    monkeypatch.setenv("LEDGER_SESSION", "forged-session")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--db", db, *args])
    return code, out.getvalue(), err.getvalue()


def managed_item(tmp_path: Path, monkeypatch, item_id: str = "item-1") -> str:
    db = str(tmp_path / "ledger.db")
    code, _out, err = invoke(
        monkeypatch,
        db,
        "new",
        "--id",
        item_id,
        "--type",
        "ticket",
        "--title",
        "Loop item",
    )
    assert code == 0, err
    with open_ledger(db) as ledger:
        manage_item(ledger, item_id, idempotency_key=f"manage:{item_id}")
    return db


def loop_events(db: str, item_id: str = "item-1"):
    with open_ledger(db, create=False) as ledger:
        return [e for e in ledger.events(item_id=item_id) if e.kind.startswith("loop.")]


def write_crafted_loop_export(export_dir: Path, item_id: str = "forged") -> None:
    """Write a valid hash chain whose loop events have no writer proof."""
    payloads = (
        ("item.created", {"type": "ticket", "title": "Forged loop item"}),
        ("loop.managed", {}),
        ("loop.intent", {"operation": "merge", "operation_id": "merge-1"}),
        (
            "loop.done",
            {"operation": "merge", "operation_id": "merge-1", "intent_event_id": 3},
        ),
        ("loop.closed", {"merge_done_event_id": 4}),
    )
    export_dir.mkdir()
    previous = GENESIS_HASH
    for event_id, (kind, payload) in enumerate(payloads, 1):
        event = Event(
            id=event_id,
            kind=kind,
            payload_version=1,
            payload=payload,
            item_id=item_id,
            idempotency_key=f"forged-{event_id}",
            actor="forged-loop-writer",
            harness="foreign-writer",
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
    (export_dir / "watermark.json").write_text('{"id":5}\n', encoding="utf-8")


def test_foreign_state_cannot_close_loop_item(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)

    code, _out, err = invoke(monkeypatch, db, "state", "item-1", "--status", "closed")

    assert code == 3, "foreign item.state closed must be refused"
    assert "reserved-transition:loop-close" in err
    with open_ledger(db, create=False) as ledger:
        assert ledger.items()[0]["status"] == "open", "generic state must not close a loop item"


@pytest.mark.parametrize("status", ["open", "claimed", "blocked"])
def test_foreign_state_cannot_reopen_loop_closed_item(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        start_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        done = finish_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        close_item(ledger, "item-1", done.id, idempotency_key="loop:item-1:closed")

    code, _out, err = invoke(monkeypatch, db, "state", "item-1", "--status", status)

    assert code == 3, f"foreign item.state {status} must not reopen a loop-closed item"
    assert "reserved-transition:loop-close" in err
    with open_ledger(db, create=False) as ledger:
        assert ledger.items()[0]["status"] == "closed", "loop.closed must remain authoritative"


def test_loop_closed_view_ignores_persisted_later_generic_state(
    tmp_path: Path, monkeypatch
) -> None:
    """A state persisted by the former bug cannot override loop.closed."""
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        start_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        done = finish_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        closed = close_item(ledger, "item-1", done.id, idempotency_key="loop:item-1:closed")
        legacy_id = closed.id + 1
        ledger._conn.execute(
            "INSERT INTO events "
            "(id, kind, payload_version, payload, item_id, idempotency_key, "
            "recorded_at, prev_hash, hash) VALUES (?, 'item.state', 1, ?, ?, ?, ?, ?, ?)",
            (
                legacy_id,
                '{"status":"open"}',
                "item-1",
                "legacy-reopen",
                "2026-01-01T00:00:09.000Z",
                closed.hash,
                "f" * 64,
            ),
        )

        assert ledger.items()[0]["status"] == "closed", "loop.closed must win in the view"


def test_closed_state_before_management_is_not_authoritative(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "ledger.db")
    code, _out, err = invoke(
        monkeypatch, db, "new", "--id", "item-1", "--type", "ticket", "--title", "Loop item"
    )
    assert code == 0, err
    code, _out, err = invoke(monkeypatch, db, "state", "item-1", "--status", "closed")
    assert code == 0, err
    with open_ledger(db, create=False) as ledger:
        manage_item(ledger, "item-1", idempotency_key="manage:item-1")
        assert ledger.items()[0]["status"] == "open", "management must invalidate generic close"


def test_management_refuses_preexisting_pr_link(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "ledger.db")
    code, _out, err = invoke(
        monkeypatch, db, "new", "--id", "item-1", "--type", "ticket", "--title", "Loop item"
    )
    assert code == 0, err
    code, _out, err = invoke(
        monkeypatch, db, "link", "item-1", "--rel", "pr", "--target", "org/repo#9"
    )
    assert code == 0, err

    with open_ledger(db, create=False) as ledger:
        with pytest.raises(ValidationError, match="loop-pr-link-already-exists"):
            manage_item(ledger, "item-1", idempotency_key="manage:item-1")
        assert not any(event.kind == "loop.managed" for event in ledger.events(item_id="item-1"))


def test_restore_refuses_crafted_loop_events(tmp_path: Path) -> None:
    export_dir = tmp_path / "crafted-export"
    write_crafted_loop_export(export_dir)
    dest = tmp_path / "restored.db"

    with pytest.raises(LedgerError, match="--accept-loop-events"):
        restore_export(export_dir, dest, system="home")

    assert not dest.exists(), "refused restore must not leave a database behind"

    (export_dir / STREAM_DIGEST_NAME).write_text(
        '{"algorithm":"sha256","digest":"not-the-export","events":5}\n',
        encoding="utf-8",
    )
    with pytest.raises(LedgerError, match="stream digest does not match"):
        restore_export(export_dir, dest, system="home", accept_loop_events=True)
    assert not dest.exists(), "forged export must not leave a database behind"

    with open_ledger(tmp_path / "direct-replay.db") as ledger:
        with pytest.raises(LedgerError, match="replay refuses reserved event"):
            replay_events(ledger, verify_export(export_dir))
        assert ledger.events() == []


def test_managed_export_restore_requires_intent_and_records_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    db = managed_item(tmp_path, monkeypatch)
    claim_database(db, "home")
    with open_ledger(db, create=False) as ledger:
        start_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        done = finish_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        close_item(ledger, "item-1", done.id, idempotency_key="loop:item-1:closed")
        original = ledger.events()
        export_dir = tmp_path / "managed-export"
        export_events(ledger, export_dir)

    refused_dest = tmp_path / "refused.db"
    with pytest.raises(LedgerError, match="--accept-loop-events"):
        restore_export(export_dir, refused_dest, system="home")
    assert not refused_dest.exists()

    dest = tmp_path / "restored.db"
    result = restore_export(export_dir, dest, system="home", accept_loop_events=True)
    assert result.events == len(original) + 1
    with open_ledger(dest, create=False) as restored:
        restored.verify(system="home")
        events = restored.events()
    assert events[:-1] == original
    provenance = events[-1]
    assert provenance.kind == "session.opened"
    assert provenance.provenance_class == "restored-export"
    assert provenance.payload["purpose"] == "restore loop-managed export"
    assert provenance.payload["source"].startswith("sha256:")


def test_forged_link_and_merged_note_cannot_mark_merge_done(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)

    link_code, _out, link_err = invoke(
        monkeypatch, db, "link", "item-1", "--rel", "pr", "--target", "org/repo#9"
    )
    note_code, _out, note_err = invoke(
        monkeypatch, db, "note", "item-1", "--text", "work:merged pr=org/repo#9"
    )

    assert link_code == 3, "foreign PR link must be refused for a loop item"
    assert "reserved-transition:loop-pr" in link_err
    assert note_code == 3, "legacy merged marker must be refused"
    assert "reserved-note-prefix" in note_err
    assert not any(
        e.kind == "loop.done" for e in loop_events(db)
    ), "forgery must not record merge done"


def test_generic_cli_cannot_forge_passing_review_event(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)
    payload = json.dumps(
        {
            "operation": "review",
            "operation_id": "review-1",
            "intent_event_id": 1,
            "verdict": "pass",
        }
    )

    code, _out, err = invoke(
        monkeypatch,
        db,
        "append",
        "--kind",
        "loop.done",
        "--item",
        "item-1",
        "--payload",
        payload,
    )

    assert code == 3, "generic append must not forge a passing review"
    assert "reserved-kind:loop.done" in err


def test_library_caller_cannot_acquire_loop_writer_capability(tmp_path: Path) -> None:
    with open_ledger(tmp_path / "ledger.db") as ledger:
        assert not hasattr(ledger, "append_internal"), "the former capability grant must be gone"
        with pytest.raises(ValidationError, match="reserved-kind:loop.managed"):
            ledger.append(
                kind="loop.managed",
                payload={},
                item_id="forged",
                idempotency_key="forged-loop-event",
            )
        with pytest.raises(TypeError, match="_capability"):
            ledger.append(
                kind="loop.managed",
                payload={},
                item_id="forged",
                idempotency_key="forged-capability",
                _capability=object(),
            )
        with pytest.raises(ValidationError, match="loop-writer-capability-required"):
            ledger._append_loop_event(
                object(),
                kind="loop.managed",
                payload={},
                item_id="forged",
                idempotency_key="forged-private-capability",
            )
        assert ledger.events() == []


def test_forged_built_and_pass_notes_are_refused(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)

    results = [
        invoke(monkeypatch, db, "note", "item-1", "--text", text)
        for text in ("work:built pr=org/repo#9", "work:review verdict=pass")
    ]

    assert all(code == 3 for code, _out, _err in results), "all legacy loop notes must be refused"
    assert all("reserved-note-prefix" in err for _code, _out, err in results)
    assert not any(
        e.kind == "loop.done" for e in loop_events(db)
    ), "notes must not forge done events"


@pytest.mark.parametrize(
    "text",
    [
        "Work:merged pr=org/repo#9",
        " work:merged pr=org/repo#9",
        "\nLOOP: review pass",
        "ｗｏｒｋ：merged pr=org/repo#9",
    ],
)
def test_reserved_note_prefix_is_normalized(tmp_path: Path, monkeypatch, text: str) -> None:
    db = managed_item(tmp_path, monkeypatch)

    code, _out, err = invoke(monkeypatch, db, "note", "item-1", "--text", text)

    assert code == 3, "normalized legacy loop note must be refused"
    assert "reserved-note-prefix" in err


@pytest.mark.parametrize(
    "invisible",
    ["\u200b", "\u200d", "\u00ad", "\ufeff", "\ufe0f"],
)
def test_reserved_note_prefix_ignores_default_ignorable_characters(
    tmp_path: Path, monkeypatch, invisible: str
) -> None:
    db = managed_item(tmp_path, monkeypatch)

    code, _out, err = invoke(
        monkeypatch,
        db,
        "note",
        "item-1",
        "--text",
        f"{invisible}work:merged pr=org/repo#9",
    )

    assert code == 3, "invisible characters must not disguise a reserved note prefix"
    assert "reserved-note-prefix" in err


def test_build_intent_survives_crash_without_second_start(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        first = start_operation(ledger, "item-1", "build", "build-1", head="old")
        resumed = operation_state(ledger, "item-1", "build")
        retry = start_operation(ledger, "item-1", "build", "build-1", head="old")

    assert resumed.state == "started", "crash resume must see an unfinished build"
    assert resumed.intent == first
    assert retry.id == first.id, "retry must reuse the write-ahead build intent"


def test_merge_intent_survives_crash_without_second_start(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        first = start_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        resumed = operation_state(ledger, "item-1", "merge")
        retry = start_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")

    assert resumed.state == "started", "crash resume must reconcile an unfinished merge"
    assert resumed.intent == first
    assert retry.id == first.id, "retry must reuse the write-ahead merge intent"


def test_operation_retry_survives_changed_process_attribution(
    tmp_path: Path, monkeypatch
) -> None:
    db = managed_item(tmp_path, monkeypatch)
    original_attribution = {
        "actor": "loop-actor-1",
        "harness": "loop-harness-1",
        "model": "loop-model-1",
        "session": "loop-session-1",
    }
    resumed_attribution = {
        "actor": "loop-actor-2",
        "harness": "loop-harness-2",
        "model": "loop-model-2",
        "session": "loop-session-2",
    }
    with open_ledger(db, create=False) as ledger:
        intent = start_operation(
            ledger,
            "item-1",
            "build",
            "build-1",
            head="old",
            **original_attribution,
        )
        done = finish_operation(
            ledger,
            "item-1",
            "build",
            "build-1",
            head="old",
            outcome="built",
            **original_attribution,
        )

        retried_intent = start_operation(
            ledger,
            "item-1",
            "build",
            "build-1",
            head="old",
            **resumed_attribution,
        )
        retried_done = finish_operation(
            ledger,
            "item-1",
            "build",
            "build-1",
            head="old",
            outcome="built",
            **resumed_attribution,
        )

        assert retried_intent == intent
        assert retried_done == done

        with pytest.raises(IdempotencyConflict, match="already recorded"):
            start_operation(
                ledger,
                "item-1",
                "build",
                "build-1",
                head="new",
                **resumed_attribution,
            )
        with pytest.raises(IdempotencyConflict, match="already recorded"):
            finish_operation(
                ledger,
                "item-1",
                "build",
                "build-1",
                head="old",
                outcome="failed",
                **resumed_attribution,
            )




def test_manage_retry_survives_changed_process_attribution(tmp_path: Path) -> None:
    db = str(tmp_path / "manage-retry.db")
    with open_ledger(db) as ledger:
        ledger.append(
            kind="item.created",
            payload={"type": "ticket", "title": "Retry management"},
            item_id="item-1",
            idempotency_key="created",
        )
        first = manage_item(
            ledger,
            "item-1",
            idempotency_key="manage:item-1",
            actor="first",
            harness="h1",
            model="m1",
            session="s1",
        )
        retried = manage_item(
            ledger,
            "item-1",
            idempotency_key="manage:item-1",
            actor="resumed",
            harness="h2",
            model="m2",
            session="s2",
        )
    assert retried == first


def test_close_retry_survives_changed_process_attribution(
    tmp_path: Path, monkeypatch
) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        start_operation(ledger, "item-1", "merge", "merge-1")
        merged = finish_operation(ledger, "item-1", "merge", "merge-1")
        first = close_item(
            ledger,
            "item-1",
            merged.id,
            idempotency_key="close:item-1",
            outcome="shipped",
            actor="first",
            harness="h1",
            model="m1",
            session="s1",
        )
        retried = close_item(
            ledger,
            "item-1",
            merged.id,
            idempotency_key="close:item-1",
            outcome="shipped",
            actor="resumed",
            harness="h2",
            model="m2",
            session="s2",
        )
    assert retried == first


def test_done_requires_matching_write_ahead_intent(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(ValidationError, match="loop-intent-missing"):
            finish_operation(ledger, "item-1", "build", "missing", target="org/repo#1")


def test_start_operation_requires_managed_item(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "ledger.db")
    code, _out, err = invoke(
        monkeypatch, db, "new", "--id", "item-1", "--type", "ticket", "--title", "Loop item"
    )
    assert code == 0, err

    with open_ledger(db, create=False) as ledger:
        with pytest.raises(ValidationError, match="loop-item-not-managed"):
            start_operation(ledger, "item-1", "build", "build-1", head="old")


def test_done_intent_must_belong_to_same_item(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch, item_id="item-1")
    code, _out, err = invoke(
        monkeypatch, db, "new", "--id", "item-2", "--type", "ticket", "--title", "Other item"
    )
    assert code == 0, err
    with open_ledger(db, create=False) as ledger:
        manage_item(ledger, "item-2", idempotency_key="manage:item-2")
        other_intent = start_operation(ledger, "item-2", "build", "build-1", head="old")
        monkeypatch.setattr(
            "ledger.loop.operation_state",
            lambda *_args, **_kwargs: OperationState("started", other_intent, None),
        )
        with pytest.raises(ValidationError, match="loop-intent-mismatch"):
            finish_operation(ledger, "item-1", "build", "build-1", head="old")


def test_done_completes_write_ahead_operation(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        start_operation(ledger, "item-1", "build", "build-1", head="old")
        done = finish_operation(ledger, "item-1", "build", "build-1", target="org/repo#1")
        resumed = operation_state(ledger, "item-1", "build", "build-1")

    assert resumed.state == "finished", "done must finish the write-ahead operation"
    assert resumed.done == done


def test_internal_merge_done_can_close_loop_item(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        start_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        done = finish_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        close_item(
            ledger,
            "item-1",
            done.id,
            idempotency_key="loop:item-1:closed",
            outcome="merged",
        )
        assert ledger.items()[0]["status"] == "closed"


def test_close_requires_done_event_not_merge_intent(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        intent = start_operation(ledger, "item-1", "merge", "merge-1", target="org/repo#1")
        with pytest.raises(ValidationError, match="loop-merge-done-required"):
            close_item(
                ledger,
                "item-1",
                intent.id,
                idempotency_key="loop:item-1:closed",
            )


def test_card_decision_rows_include_asked_event_id(tmp_path: Path, monkeypatch) -> None:
    db = managed_item(tmp_path, monkeypatch)
    with open_ledger(db, create=False) as ledger:
        asked = ledger.append(
            kind="decision.asked",
            payload={"question": "Merge?", "options": [{"label": "yes"}]},
            item_id="item-1",
            idempotency_key="ask-1",
        )
        ledger.append(
            kind="decision.elected",
            payload={
                "question": "Merge?",
                "chosen": "yes",
                "chosen_description": "",
                "rejected": [],
                "scope": "merge",
                "quote_ref": 1,
                "asked_event_id": asked.id,
            },
            item_id="item-1",
            idempotency_key="elect-1",
        )
        from ledger.card import project_card

        card = project_card(ledger, "item-1")
    assert card.decisions[0]["asked_event_id"] == asked.id


@pytest.mark.parametrize("seed", (7, 410, 20261008))
def test_seeded_generic_cli_sequences_cannot_close_link_merge_or_forge_marker(
    tmp_path: Path, monkeypatch, seed: int
) -> None:
    rng = random.Random(seed)
    db = str(tmp_path / f"property-{seed}.db")
    crafted_export = tmp_path / f"property-crafted-export-{seed}"
    write_crafted_loop_export(crafted_export, item_id="restored-forgery")
    write_stream_digest(crafted_export, verify_export(crafted_export))
    # ``import`` is a toolkit plugin verb, not a ledger verb; the toolkit's
    # own suite runs the same check for it.
    action_names = (
        "append",
        "append-many",
        "claim",
        "elect",
        "guidance",
        "link",
        "new",
        "note",
        "pick",
        "restore",
        "spec",
        "state",
    )
    cli_writers = {name for name, verb in discover_verbs().items() if verb.writes}
    assert set(action_names) == cli_writers | {"restore"}

    for sequence in range(24):
        item_id = f"property-{sequence}"
        code, _out, err = invoke(
            monkeypatch,
            db,
            "new",
            "--id",
            item_id,
            "--type",
            "ticket",
            "--title",
            f"Property {sequence}",
        )
        assert code == 0, err
        with open_ledger(db) as ledger:
            managed = manage_item(ledger, item_id, idempotency_key=f"manage:{item_id}")

        quote = f"operator chose safe {seed}-{sequence}"
        code, out, err = invoke(
            monkeypatch,
            db,
            "--json",
            "append",
            "--kind",
            "utterance.captured",
            "--item",
            item_id,
            "--payload",
            json.dumps({"text": quote}),
        )
        assert code == 0, err
        quote_ref = json.loads(out)["data"]["event"]["id"]

        generated = list(action_names)
        generated.extend(rng.choices(action_names, k=rng.randint(1, 5)))
        rng.shuffle(generated)
        for step, action in enumerate(generated):
            suffix = f"{seed}-{sequence}-{step}"
            if action == "append":
                reserved_payloads = {
                    "loop.managed": {},
                    "loop.intent": {"operation": "merge", "operation_id": suffix},
                    "loop.done": {
                        "operation": "merge",
                        "operation_id": suffix,
                        "intent_event_id": 1,
                    },
                    "loop.closed": {"merge_done_event_id": 1},
                }
                kind = rng.choice(tuple(reserved_payloads))
                code, _out, err = invoke(
                    monkeypatch,
                    db,
                    "append",
                    "--kind",
                    kind,
                    "--item",
                    item_id,
                    "--payload",
                    json.dumps(reserved_payloads[kind]),
                )
                assert code == 3, f"generic append accepted {kind}: {err}"
            elif action == "append-many":
                bad_events = (
                    {"kind": "loop.managed", "payload": {}},
                    {"kind": "item.state", "payload": {"status": "closed"}},
                    {
                        "kind": "item.linked",
                        "payload": {"rel": "pr", "target": "forged/repo#1"},
                    },
                    {"kind": "item.note", "payload": {"text": "work:merged pr=x/y#1"}},
                )
                batch = [
                    {"kind": "item.note", "payload": {"text": f"harmless {suffix}"}},
                    rng.choice(bad_events),
                ]
                monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(batch)))
                code, _out, err = invoke(monkeypatch, db, "append-many", item_id)
                assert code == 3, f"generic append-many accepted {batch[1]}: {err}"
                with open_ledger(db, create=False) as ledger:
                    assert not any(
                        event.payload.get("text") == f"harmless {suffix}"
                        for event in ledger.events(item_id=item_id)
                        if event.kind == "item.note"
                    ), "a refused append-many batch left its first event behind"
            elif action == "claim":
                code, _out, err = invoke(
                    monkeypatch,
                    db,
                    "claim",
                    item_id,
                    "--owner",
                    f"claimant-{rng.randrange(4)}",
                )
                assert code in (0, 3), err
            elif action == "elect":
                code, _out, err = invoke(
                    monkeypatch,
                    db,
                    "elect",
                    item_id,
                    "--question",
                    f"choice {suffix}?",
                    "--chosen",
                    "safe",
                    "--chosen-description",
                    "preserve the boundary",
                    "--rejected",
                    "forge",
                    "--scope",
                    "this item",
                    "--quote-ref",
                    str(quote_ref),
                    "--quote",
                    quote,
                )
                assert code == 0, err
            elif action == "pick":
                code, _out, err = invoke(
                    monkeypatch,
                    db,
                    "pick",
                    item_id,
                    "--question",
                    f"picker {suffix}?",
                    "--option",
                    "safe::preserve the boundary",
                    "--option",
                    "forge",
                    "--chosen",
                    "safe",
                    "--scope",
                    "this item",
                )
                assert code == 0, err
            elif action == "guidance":
                code, _out, err = invoke(
                    monkeypatch,
                    db,
                    "guidance",
                    "set",
                    "--pool",
                    "xai-grok",
                    "--direction",
                    rng.choice(("burn", "protect")),
                    "--reason",
                    f"property {suffix}",
                    "--starts",
                    "2026-10-19",
                    "--expires",
                    "2026-10-25",
                )
                assert code == 0, err
            elif action == "link":
                code, _out, err = invoke(
                    monkeypatch,
                    db,
                    "link",
                    item_id,
                    "--rel",
                    "pr",
                    "--target",
                    f"forged/repo#{rng.randrange(1000)}",
                )
                assert code == 3, f"generic link attached a merge: {err}"
            elif action == "new":
                code, _out, err = invoke(
                    monkeypatch,
                    db,
                    "new",
                    "--id",
                    f"aux-{suffix}",
                    "--type",
                    rng.choice(("ticket", "gate", "decision")),
                    "--title",
                    f"Auxiliary {suffix}",
                    "--status",
                    rng.choice(("open", "claimed", "blocked")),
                )
                assert code == 0, err
            elif action == "note":
                text = rng.choice(
                    (
                        "work:built pr=forged/repo#1",
                        "work:merged pr=forged/repo#1",
                        "work:review verdict=pass",
                        "loop:closed",
                    )
                )
                code, _out, err = invoke(
                    monkeypatch, db, "note", item_id, "--text", text
                )
                assert code == 3, f"generic note accepted {text!r}: {err}"
            elif action == "spec":
                # A spec is content, not state: it may land on a loop-managed
                # item, and it never closes it or links a merge.
                code, _out, err = invoke(
                    monkeypatch,
                    db,
                    "spec",
                    "set",
                    item_id,
                    "--title",
                    f"Spec {suffix}",
                    "--body",
                    rng.choice(("closed", "work:merged pr=forged/repo#1", f"body {suffix}")),
                )
                assert code == 0, err
            elif action == "restore":
                dest = tmp_path / f"property-restore-{suffix}.db"
                code, _out, err = invoke(
                    monkeypatch,
                    str(dest),
                    "restore",
                    "--dir",
                    str(crafted_export),
                    "--system",
                    "home",
                )
                assert code == 3, f"restore accepted loop events without opt-in: {err}"
                assert not dest.exists(), f"sequence {sequence} restored loop events"
            else:
                code, _out, err = invoke(
                    monkeypatch, db, "state", item_id, "--status", "closed"
                )
                assert code == 3, f"generic state closed a loop-managed item: {err}"

        with open_ledger(db, create=False) as ledger:
            item = next(row for row in ledger.items() if row["item_id"] == item_id)
            reserved = [
                event
                for event in ledger.events(item_id=item_id)
                if event.kind.startswith("loop.")
            ]
            pr_links = [
                event
                for event in ledger.events(item_id=item_id)
                if event.kind == "item.linked" and event.payload["rel"] == "pr"
            ]
        assert item["status"] != "closed", f"sequence {sequence} closed {item_id}"
        assert [event.id for event in reserved] == [managed.id], (
            f"sequence {sequence} wrote loop-reserved events: {reserved}"
        )
        assert pr_links == [], f"sequence {sequence} attached a merge link"


def test_cli_restore_opt_in_accepts_reserved_events_and_warns(
    tmp_path: Path, monkeypatch
) -> None:
    export_dir = tmp_path / "accepted-export"
    write_crafted_loop_export(export_dir)
    write_stream_digest(export_dir, verify_export(export_dir))
    dest = tmp_path / "accepted.db"

    code, out, err = invoke(
        monkeypatch,
        str(dest),
        "restore",
        "--dir",
        str(export_dir),
        "--system",
        "home",
        "--accept-loop-events",
    )

    assert code == 0, err
    # Five exported events, the restore receipt, and the claim for home.
    assert out == f"restored 7 events into {dest}\n"
    assert err == (
        "restore: accepting 4 loop-reserved events "
        "(kinds: loop.closed, loop.done, loop.intent, loop.managed) "
        "because --accept-loop-events was given\n"
    )
    with open_ledger(dest, create=False) as ledger:
        assert [event.kind for event in ledger.events() if event.kind.startswith("loop.")] == [
            "loop.managed",
            "loop.intent",
            "loop.done",
            "loop.closed",
        ]
