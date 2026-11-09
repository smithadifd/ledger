"""Decision records, the standing guard, resume-card sections, decay, picker
elections and separate system databases, on a synthetic fixture ticket."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ledger import (
    IdempotencyConflict,
    Ledger,
    LedgerError,
    SharedDatabaseError,
    UnsafeDatabaseFile,
    claim_database,
    open_ledger,
    resolve_system_paths,
)
from ledger.card import format_card, project_card
from ledger.cli import main
from ledger.export import (
    export_events,
    hash_body,
    item_export,
    load_export,
    verify_export,
    write_event_files,
    write_watermark,
)
from ledger.loop import _LOOP_WRITER_CAPABILITY
from ledger.restore import replay_events, restore_export
from ledger.store import GENESIS_HASH, Event, event_digest
from ledger.schema import (
    DURABILITY,
    EPHEMERAL,
    ITEM_STATUSES,
    STANDINGS,
    ValidationError,
    durability,
    is_ephemeral,
    known_kinds,
)
from ledger.verbs import CliError
from ledger.verbs.pick import TOOL_ANSWER, record_picker_answer

FIXTURE = Path(__file__).parent / "fixtures" / "decisions" / "ticket.json"
ITEM = "TK-100"


def _resolve(value: Any, ids: dict[str, int]) -> Any:
    if isinstance(value, str) and value.startswith("@"):
        return ids[value[1:]]
    if isinstance(value, dict):
        return {key: _resolve(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, ids) for item in value]
    return value


def load_fixture(lg) -> dict[str, int]:
    """Append the fixture ticket; return each fixture key's event id."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ids: dict[str, int] = {}
    for spec in data["events"]:
        event = lg.append(
            kind=spec["kind"],
            payload=_resolve(spec["payload"], ids),
            item_id=data["item_id"],
            idempotency_key=f"fixture:{spec['key']}",
            harness="pytest",
            provenance_class=spec.get("provenance_class"),
        )
        ids[spec["key"]] = event.id
    return ids


@pytest.fixture
def ledger(tmp_path: Path):
    lg = open_ledger(tmp_path / "ledger.db")
    yield lg
    lg.close()


@pytest.fixture
def ticket(ledger):
    return load_fixture(ledger)


def _record(lg, event_id: int) -> dict[str, Any]:
    return next(row for row in lg.decision_records() if row["decision_event_id"] == event_id)


def _close(lg, item_id: str = ITEM) -> None:
    lg.append(
        kind="item.state",
        payload={"status": "closed"},
        item_id=item_id,
        idempotency_key=f"close:{item_id}",
        harness="pytest",
    )


def invoke(monkeypatch, argv, env=None):
    merged = {"LEDGER_HARNESS": "pytest", "LEDGER_POINTER_SIDECAR": None}
    merged.update(env or {})
    for key, value in merged.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


# --- decision records carry every field ------------------------------------


def test_fixture_decision_record_carries_every_field(ledger, ticket) -> None:
    record = _record(ledger, ticket["store"])
    assert record["item_id"] == ITEM
    assert record["who"] == "the owner"
    assert record["who_kind"] == "person"
    assert record["standing"] == "settled"
    assert record["source_event_id"] == ticket["reply"]
    assert record["standing_event_id"] == ticket["store-settled"]
    assert record["effect"].startswith("Resume reads the shared store first")
    assert record["tags"] == [
        {"type": "ticket", "id": ITEM},
        {"type": "initiative", "id": "INIT-STORE"},
    ]
    assert record["verbatim"] == "I lean towards the shared store."
    assert record["phase"] == "plan"
    # The result's evidence pointer wins over the record's own.
    assert record["evidence"] == "example-org/example-repo#7"
    assert record["result"] == "Resume took one read instead of three."


def test_role_position_is_recorded_as_relayed(ledger, ticket) -> None:
    record = _record(ledger, ticket["naming-2"])
    assert (record["who"], record["who_kind"], record["relayed_by"]) == (
        "tech lead", "role", "the owner",
    )
    payload = dict(json.loads(FIXTURE.read_text())["events"][5]["payload"])
    payload.pop("relayed_by")
    with pytest.raises(ValidationError, match="role-needs-relayed-by"):
        ledger.append(kind="decision.recorded", payload=payload, item_id=ITEM,
                      idempotency_key="role-unrelayed")


def test_superseded_record_links_its_replacement(ledger, ticket) -> None:
    record = _record(ledger, ticket["naming"])
    assert record["standing"] == "superseded"
    assert record["superseded_by"] == ticket["naming-2"]


def test_decisions_are_found_by_tag_across_items(ledger, ticket) -> None:
    ledger.append(kind="item.created", payload={"type": "ticket", "title": "Other"},
                  item_id="TK-200", idempotency_key="other")
    ledger.append(
        kind="decision.recorded",
        payload={
            "who": "the owner", "who_kind": "person", "standing": "lean",
            "effect": "Same store for the sibling ticket.",
            "tags": [{"type": "ticket", "id": "TK-200"}, {"type": "initiative", "id": "INIT-STORE"}],
            "verbatim": "Same here.", "phase": "orient",
        },
        item_id="TK-200",
        idempotency_key="sibling",
    )
    found = ledger.decision_records(tag=("initiative", "INIT-STORE"))
    assert [row["item_id"] for row in found] == [ITEM, "TK-200"]


# --- the lean -> settled guard ------------------------------------------------


def _settle(lg, decision_id: int, key: str, **extra):
    return lg.append(
        kind="decision.standing",
        payload={"decision_event_id": decision_id, "standing": "settled", **extra},
        item_id=ITEM,
        idempotency_key=key,
    )


def _lean_decision(lg, key: str = "lean-1") -> int:
    return lg.append(
        kind="decision.recorded",
        payload={
            "who": "the owner", "who_kind": "person", "standing": "lean",
            "effect": "Proceed with the narrow fix.",
            "tags": [{"type": "ticket", "id": ITEM}],
            "verbatim": "Probably the narrow fix.", "phase": "plan",
        },
        item_id=ITEM,
        idempotency_key=key,
    ).id


@pytest.mark.parametrize(
    "source, reason",
    [
        (None, "settled-needs-source-event"),
        (99_999, "unknown-source-event:99999"),
        ("note", "source-event-cannot-settle:item.note"),
    ],
    ids=["missing", "unknown-id", "not-an-outside-event"],
)
def test_lean_to_settled_without_source_event_is_refused(ledger, ticket, source, reason) -> None:
    decision = _lean_decision(ledger)
    extra: dict[str, Any] = {}
    if source == "note":
        extra["source_event_id"] = ledger.append(
            kind="item.note", payload={"text": "a note is not an outside event"},
            item_id=ITEM, idempotency_key="a-note",
        ).id
    elif source is not None:
        extra["source_event_id"] = source
    before = len(ledger.events())
    with pytest.raises(ValidationError, match=reason):
        _settle(ledger, decision, "settle-refused", **extra)
    assert len(ledger.events()) == before
    assert _record(ledger, decision)["standing"] == "lean"


def test_settled_record_without_source_event_is_refused(ledger, ticket) -> None:
    with pytest.raises(ValidationError, match="settled-needs-source-event"):
        ledger.append(
            kind="decision.recorded",
            payload={
                "who": "the owner", "who_kind": "person", "standing": "settled",
                "effect": "x", "tags": [{"type": "ticket", "id": ITEM}],
                "verbatim": "settled already", "phase": "close",
            },
            item_id=ITEM,
            idempotency_key="settled-no-source",
        )


@pytest.mark.parametrize("source_key", ["reply", "outside", "election"])
def test_outside_events_settle(ledger, ticket, source_key) -> None:
    decision = _lean_decision(ledger)
    if source_key == "outside":
        source = ledger.append(kind="outside.recorded",
                               payload={"what": "merge", "ref": "example-org/example-repo#7"},
                               item_id=ITEM, idempotency_key="merged").id
    elif source_key == "election":
        source = ledger.append(
            kind="decision.elected",
            payload={"question": "q?", "chosen": "a", "chosen_description": "d",
                     "rejected": ["b"], "scope": "s", "quote_ref": ticket["reply"]},
            item_id=ITEM, idempotency_key="elected",
        ).id
    else:
        source = ticket[source_key]
    _settle(ledger, decision, "settle-ok", source_event_id=source)
    record = _record(ledger, decision)
    assert (record["standing"], record["source_event_id"]) == ("settled", source)


def test_guard_holds_for_batch_and_cli_writers(ledger, ticket, tmp_path, monkeypatch) -> None:
    decision = _lean_decision(ledger)
    with pytest.raises(ValidationError, match="settled-needs-source-event"):
        ledger.append_many([{
            "kind": "decision.standing",
            "payload": {"decision_event_id": decision, "standing": "settled"},
            "item_id": ITEM,
            "idempotency_key": "batch-settle",
        }])
    db = str(ledger.path)
    argv = [
        "--db", db, "append", "--kind", "decision.standing", "--item", ITEM,
        "--payload", json.dumps({"decision_event_id": decision, "standing": "settled"}),
    ]
    code, _out, err = invoke(monkeypatch, argv)
    assert code == 3 and "db_fd_unknown" in err
    assert _record(ledger, decision)["standing"] == "lean"
    ledger.close()
    code, _out, err = invoke(monkeypatch, argv)
    assert code == 3, err
    assert "settled-needs-source-event" in err


def test_superseded_needs_a_decision_link_and_is_final(ledger, ticket) -> None:
    decision = _lean_decision(ledger)
    with pytest.raises(ValidationError, match="superseded-needs-link"):
        ledger.append(kind="decision.standing",
                      payload={"decision_event_id": decision, "standing": "superseded"},
                      item_id=ITEM, idempotency_key="sup-1")
    with pytest.raises(ValidationError, match="unknown-decision"):
        ledger.append(kind="decision.standing",
                      payload={"decision_event_id": decision, "standing": "superseded",
                               "superseded_by": ticket["reply"]},
                      item_id=ITEM, idempotency_key="sup-2")
    with pytest.raises(ValidationError, match="decision-superseded"):
        _settle(ledger, ticket["naming"], "settle-superseded", source_event_id=ticket["reply"])


def _reply(lg, item_id: str, key: str, provenance_class: str | None = "submitted-prompt") -> int:
    return lg.append(kind="utterance.captured", payload={"text": "Yes, go ahead."},
                     item_id=item_id, idempotency_key=key,
                     provenance_class=provenance_class).id


def _other_ticket(lg, item_id: str = "TK-200") -> str:
    lg.append(kind="item.created", payload={"type": "ticket", "title": "Unrelated"},
              item_id=item_id, idempotency_key=f"create:{item_id}")
    return item_id


def test_source_event_on_an_unrelated_item_is_refused(ledger, ticket) -> None:
    other = _other_ticket(ledger)
    elsewhere = _reply(ledger, other, "reply-elsewhere")
    decision = _lean_decision(ledger)
    with pytest.raises(ValidationError, match=f"source-event-other-item:{elsewhere}"):
        _settle(ledger, decision, "settle-elsewhere", source_event_id=elsewhere)
    assert _record(ledger, decision)["standing"] == "lean"
    with pytest.raises(ValidationError, match="source-event-other-item"):
        ledger.append(
            kind="decision.recorded",
            payload={"who": "the owner", "who_kind": "person", "standing": "settled",
                     "effect": "x", "tags": [{"type": "ticket", "id": ITEM}],
                     "verbatim": "settled from elsewhere", "phase": "plan",
                     "source_event_id": elsewhere},
            item_id=ITEM, idempotency_key="born-settled-elsewhere",
        )
    # An outside event on no item is not on the decision's item either.
    unattached = ledger.append(kind="outside.recorded",
                               payload={"what": "merge", "ref": "example-org/example-repo#9"},
                               idempotency_key="merge-no-item").id
    with pytest.raises(ValidationError, match="source-event-other-item"):
        _settle(ledger, decision, "settle-unattached", source_event_id=unattached)


def test_source_event_on_a_tagged_ticket_settles(ledger, ticket) -> None:
    other = _other_ticket(ledger)
    elsewhere = _reply(ledger, other, "reply-tagged")
    decision = ledger.append(
        kind="decision.recorded",
        payload={"who": "the owner", "who_kind": "person", "standing": "lean",
                 "effect": "Both tickets use the shared store.",
                 "tags": [{"type": "ticket", "id": ITEM}, {"type": "ticket", "id": other}],
                 "verbatim": "Same store for both.", "phase": "plan"},
        item_id=ITEM, idempotency_key="lean-spanning",
    ).id
    _settle(ledger, decision, "settle-tagged", source_event_id=elsewhere)
    assert _record(ledger, decision)["source_event_id"] == elsewhere
    # A non-ticket tag with the same id is not the declared link.
    initiative_only = ledger.append(
        kind="decision.recorded",
        payload={"who": "the owner", "who_kind": "person", "standing": "lean",
                 "effect": "x", "tags": [{"type": "initiative", "id": other}],
                 "verbatim": "x", "phase": "plan"},
        item_id=ITEM, idempotency_key="lean-initiative-tag",
    ).id
    with pytest.raises(ValidationError, match="source-event-other-item"):
        _settle(ledger, initiative_only, "settle-initiative-tag", source_event_id=elsewhere)


@pytest.mark.parametrize("provenance", [None, "generated", "draft", "imported", "restored-export"])
def test_generated_utterance_cannot_settle(ledger, ticket, provenance) -> None:
    generated = _reply(ledger, ITEM, "generated-reply", provenance_class=provenance)
    decision = _lean_decision(ledger)
    with pytest.raises(ValidationError, match="source-utterance-not-a-reply"):
        _settle(ledger, decision, "settle-generated", source_event_id=generated)
    assert _record(ledger, decision)["standing"] == "lean"


@pytest.mark.parametrize("provenance", ["submitted-prompt", "tool-answer"])
def test_person_utterance_settles(ledger, ticket, provenance) -> None:
    reply = _reply(ledger, ITEM, "person-reply", provenance_class=provenance)
    decision = _lean_decision(ledger)
    _settle(ledger, decision, "settle-person", source_event_id=reply)
    assert _record(ledger, decision)["standing"] == "settled"


# --- restore replays through the same guard ---------------------------------


def _hand_built_export(export_dir: Path, decision_payload: dict[str, Any]) -> None:
    """Write a hash-chained export by hand, bypassing the append guard."""
    specs = [
        ("item.created", {"type": "ticket", "title": "Hand-built"}),
        ("decision.recorded", decision_payload),
    ]
    events: list[Event] = []
    prev = GENESIS_HASH
    for index, (kind, payload) in enumerate(specs, start=1):
        draft = Event(id=index, kind=kind, payload_version=1, payload=payload, item_id=ITEM,
                      idempotency_key=f"hand-{index}", actor=None, harness="pytest",
                      model=None, session=None, channel=None, provenance_class=None,
                      recorded_at="2026-01-01T00:00:00.000Z", prev_hash=prev, hash="")
        event = replace(draft, hash=event_digest(hash_body(draft)))
        events.append(event)
        prev = event.hash
    write_event_files(export_dir, events)
    write_watermark(export_dir, events[-1].id)


_HAND_DECISION = {"who": "the owner", "who_kind": "person", "effect": "x",
                  "tags": [{"type": "ticket", "id": ITEM}], "verbatim": "x", "phase": "plan"}


def test_restore_refuses_a_settled_decision_without_source(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    _hand_built_export(export_dir, {**_HAND_DECISION, "standing": "settled"})
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises(LedgerError, match="restore refuses event 2 .*settled-needs-source-event"):
        restore_export(export_dir, dest, system="home")
    assert not any(dest.parent.glob("ledger.db*")), "a refused restore left files behind"


def test_restore_accepts_the_same_export_with_a_lean_decision(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    _hand_built_export(export_dir, {**_HAND_DECISION, "standing": "lean"})
    result = restore_export(export_dir, tmp_path / "restored" / "ledger.db", system="home")
    # The two exported events, then the claim for home on the unclaimed export.
    assert result.events == 3


# --- a standing is never a ticket status --------------------------------------


def test_standing_is_never_a_ticket_status(ledger, ticket) -> None:
    assert not set(STANDINGS) & set(ITEM_STATUSES)
    with pytest.raises(ValidationError, match="bad-type:status"):
        ledger.append(kind="item.state", payload={"status": "settled"},
                      item_id=ITEM, idempotency_key="status-settled")
    assert ledger.item(ITEM)["status"] == "open"


# --- durability registry and decay --------------------------------------------


def test_every_kind_declares_its_durability() -> None:
    assert set(DURABILITY) == set(known_kinds())
    assert {kind for kind in DURABILITY if is_ephemeral(kind)} == {
        "item.progress", "session.opened", "session.closed",
    }
    # Decisions and claims never decay.
    for kind in ("decision.recorded", "decision.standing", "decision.elected", "item.state"):
        assert not is_ephemeral(kind)


def test_unregistered_kind_counts_as_durable() -> None:
    assert durability("kind.not.declared") != EPHEMERAL


def test_ephemeral_events_decay_out_of_export_after_close(ledger, ticket, tmp_path) -> None:
    progress = {ticket["progress-1"], ticket["progress-2"]}
    before = {record["id"] for record in item_export(ledger, ITEM)}
    assert progress <= before
    _close(ledger)
    after = {record["id"] for record in item_export(ledger, ITEM)}
    assert not progress & after
    assert {ticket["store"], ticket["store-settled"], ticket["stopped"]} <= after
    # The events stay in the store and in the backup export, which restore replays.
    assert progress <= {event.id for event in ledger.events(item_id=ITEM)}
    export_events(ledger, tmp_path / "backup")
    assert progress <= {event.id for event in load_export(tmp_path / "backup")}


def test_ephemeral_events_decay_out_of_card_after_close(ledger, ticket) -> None:
    text = format_card(project_card(ledger, ITEM))
    assert "## Progress" in text
    assert "Tests half written" in text
    _close(ledger)
    card = project_card(ledger, ITEM)
    text = format_card(card)
    assert "## Progress" not in text
    assert "Tests half written" not in text
    assert ticket["progress-2"] not in card.source_event_ids
    assert "## Decision records" in text


def test_decay_is_per_close_boundary_across_a_reopen(ledger, ticket) -> None:
    old = {ticket["progress-1"], ticket["progress-2"]}
    _close(ledger)
    ledger.append(kind="item.state", payload={"status": "open"}, item_id=ITEM,
                  idempotency_key="reopen", harness="pytest")
    new = ledger.append(kind="item.progress", payload={"text": "Reopened for a follow-up"},
                        item_id=ITEM, idempotency_key="progress-after-reopen").id
    shown = {record["id"] for record in item_export(ledger, ITEM)}
    assert not old & shown, "reopen brought decayed progress back"
    assert new in shown
    card = project_card(ledger, ITEM)
    text = format_card(card)
    assert "Reopened for a follow-up" in text
    assert "Tests half written" not in text
    assert not old & set(card.source_event_ids)
    ledger.append(kind="item.state", payload={"status": "closed"}, item_id=ITEM,
                  idempotency_key="close-again", harness="pytest")
    shown = {record["id"] for record in item_export(ledger, ITEM)}
    assert not (old | {new}) & shown
    assert "## Progress" not in format_card(project_card(ledger, ITEM))


# --- the resume card --------------------------------------------------------


def test_resume_card_lists_next_skills_and_references_by_path(ledger, ticket) -> None:
    card = project_card(ledger, ITEM)
    text = format_card(card)
    assert "## Next seat skills\n- review\n- pr\n" in text
    assert "## References\n- skills/_lib/ledger/decisions.py\n- example-org/example-repo#7\n" in text
    assert f"## Stop point\nevent={ticket['stopped']} [review]: Review passed;" in text
    assert card.as_dict()["skills"] == ["review", "pr"]


def test_resume_card_sections_from_the_fixture(ledger, ticket) -> None:
    text = format_card(project_card(ledger, ITEM))
    assert (
        f"- event={ticket['store']} settled (source event {ticket['reply']}): "
        "the owner (person) [plan] \"I lean towards the shared store.\""
    ) in text
    assert f"- event={ticket['naming']} superseded (by event {ticket['naming-2']}): " in text
    assert "tech lead (role, relayed by the owner)" in text
    assert "  tags: ticket:TK-100, initiative:INIT-STORE" in text
    assert "  result: Resume took one read instead of three." in text
    assert f"## Tried\n- event={ticket['tried']}: A per-session cache" in text
    assert f"- event={ticket['review-1']} round 1: changes\n  finding: The guard is only in the CLI" in text
    assert f"- event={ticket['review-2']} round 2: pass" in text
    assert "- branch: feature/tk-100" in text
    assert "- pr: example-org/example-repo#7" in text
    assert "## Conversation pointers" not in text


def test_reference_must_be_a_path_or_id_not_a_copy(ledger, ticket) -> None:
    with pytest.raises(ValidationError, match="not-single-line:refs"):
        ledger.append(kind="item.stopped",
                      payload={"point": "p", "phase": "build",
                               "refs": ["def f():\n    return copied_content"]},
                      item_id=ITEM, idempotency_key="copied")


@pytest.mark.parametrize("ref", [
    "skills/_lib/ledger/decisions.py",
    "skills/_lib/ledger/decisions.py:42",
    "NOTES.md",
    "/srv/checkout/notes/plan.md:10-20",
    "example-org/example-repo#7",
    "#12",
    "branch:feature/tk-100",
    "branch:main",
    "branch:release/v1.2",
    "branch:user@host-fix",
    "branch:fix+tests",
    "a/.github/workflows/ci.yml",
    "/srv/.config/app.toml",
    "c4c2f211",
    "0123456789abcdef0123456789abcdef01234567",
    "event:17",
])
def test_reference_shapes_accepted(ledger, ticket, ref) -> None:
    ledger.append(kind="item.stopped", payload={"point": "p", "phase": "build", "refs": [ref]},
                  item_id=ITEM, idempotency_key="ref-ok")


@pytest.mark.parametrize("ref", [
    "Looked at the decisions module and it seems fine",
    "done",
    "see PR 7",
    "event:abc",
    "branch:",
    "https://example.org/x",
    # Paths with a "." or ".." segment.
    "../../etc",
    "../../etc/passwd",
    "skills/../../etc/passwd",
    "./README.md",
    "a/./b.py",
    "/srv/..",
    "/srv/../etc/passwd",
    "a/..:12",
    # Branch names git refuses.
    "branch:feature..x",
    "branch:-rf",
    "branch:/main",
    "branch:main/",
    "branch:feature//x",
    "branch:main.lock",
    "branch:feature/x.lock/y",
    "branch:.hidden",
    "branch:feature/.hidden",
    "branch:main.",
    "branch:a~1",
    "branch:a^",
    "branch:a:b",
    "branch:a?",
    "branch:a*",
    "branch:a[b",
    "branch:a\\b",
    "branch:a b",
    "branch:a\tb",
    "branch:a\x07b",
    "branch:a\x7fb",
    "branch:a@{1}",
    "branch:@",
])
def test_reference_prose_refused(ledger, ticket, ref) -> None:
    with pytest.raises(ValidationError, match=r"bad-type:refs\[0\]"):
        ledger.append(kind="item.stopped",
                      payload={"point": "p", "phase": "build", "refs": [ref]},
                      item_id=ITEM, idempotency_key="ref-prose")


def test_card_without_resume_events_is_unchanged(ledger) -> None:
    ledger.append(kind="item.created", payload={"type": "ticket", "title": "plain"},
                  item_id="plain-1", idempotency_key="plain")
    text = format_card(project_card(ledger, "plain-1"))
    for heading in ("Decision records", "Tried", "Progress", "Review rounds",
                    "Stop point", "Next seat skills", "References", "Conversation pointers"):
        assert f"## {heading}" not in text


def test_conversation_pointers_come_from_an_installed_sidecar(ledger, ticket) -> None:
    def sidecar(item_id: str):
        assert item_id == ITEM
        return [{"ref": "transcripts/session-a.jsonl:120", "note": "agreed the store"}]

    text = format_card(project_card(ledger, ITEM, pointers=sidecar))
    assert "## Conversation pointers\n- transcripts/session-a.jsonl:120: agreed the store" in text


def test_cli_card_runs_the_sidecar_named_in_the_environment(ledger, ticket, tmp_path, monkeypatch) -> None:
    script = tmp_path / "sidecar.py"
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "print(json.dumps([{'ref': 'transcripts/x.jsonl:7', 'note': 'item ' + sys.argv[1]}]))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    db = str(ledger.path)
    argv = ["--db", db, "card", ITEM]
    env = {"LEDGER_POINTER_SIDECAR": str(script)}
    code, _out, err = invoke(monkeypatch, argv, env=env)
    assert code == 3 and "db_fd_unknown" in err
    ledger.close()
    code, out, err = invoke(monkeypatch, argv, env=env)
    assert code == 0, err
    assert "- transcripts/x.jsonl:7: item TK-100" in out
    code, out, err = invoke(monkeypatch, ["--db", db, "card", ITEM])
    assert code == 0, err
    assert "## Conversation pointers" not in out


# --- picker answers land as elections ---------------------------------------


PICKER_OPTIONS = [
    {"label": "The work ledger (Recommended)", "description": "One store per system"},
    {"label": "Markdown in the shared repo"},
]


def test_picker_answer_lands_as_an_election(ledger, ticket) -> None:
    asked = ledger.append(
        kind="decision.asked",
        payload={"question": "Where does the record live?",
                 "options": [{"label": option["label"]} for option in PICKER_OPTIONS]},
        item_id=ITEM, idempotency_key="ask-store",
    )
    utterance, election = record_picker_answer(
        ledger,
        item_id=ITEM,
        question="Where does the record live?",
        options=PICKER_OPTIONS,
        chosen="The work ledger (Recommended)",
        scope="decision record location",
        asked_event_id=asked.id,
        idempotency_key="pick-1",
    )
    assert utterance.kind == "utterance.captured"
    assert utterance.channel == TOOL_ANSWER
    assert utterance.provenance_class == TOOL_ANSWER
    assert "The work ledger (Recommended)" in utterance.payload["text"]
    assert election.kind == "decision.elected"
    assert election.payload["chosen"] == "The work ledger (Recommended)"
    assert election.payload["chosen_description"] == "One store per system"
    assert election.payload["rejected"] == ["Markdown in the shared repo"]
    assert election.payload["quote_ref"] == utterance.id
    assert election.payload["asked_event_id"] == asked.id
    assert not [row for row in ledger.open_decisions() if row["asked_event_id"] == asked.id]
    # An election from a picker can settle a lean decision.
    decision = _lean_decision(ledger, "lean-picked")
    _settle(ledger, decision, "settle-picked", source_event_id=election.id)
    assert _record(ledger, decision)["standing"] == "settled"


def test_typed_picker_answer_rejects_every_offered_option(ledger, ticket) -> None:
    _utterance, election = record_picker_answer(
        ledger, item_id=ITEM, question="Which store?", options=PICKER_OPTIONS,
        chosen="Neither, ask the team first", free_text="Neither, ask the team first",
        scope="store", idempotency_key="pick-typed",
    )
    assert election.payload["rejected"] == [option["label"] for option in PICKER_OPTIONS]


def test_refused_picker_answer_captures_nothing(ledger, ticket) -> None:
    before = len(ledger.events())
    with pytest.raises(CliError, match="not an open decision.asked"):
        record_picker_answer(
            ledger, item_id=ITEM, question="q?", options=PICKER_OPTIONS,
            chosen="Markdown in the shared repo", scope="s",
            asked_event_id=ticket["reply"], idempotency_key="pick-bad",
        )
    assert len(ledger.events()) == before


def _ask_store(lg) -> int:
    return lg.append(
        kind="decision.asked",
        payload={"question": "Where does the record live?",
                 "options": [{"label": option["label"]} for option in PICKER_OPTIONS]},
        item_id=ITEM, idempotency_key="ask-store",
    ).id


def _pick(lg, asked: int, chosen: str = "The work ledger (Recommended)"):
    return record_picker_answer(
        lg, item_id=ITEM, question="Where does the record live?", options=PICKER_OPTIONS,
        chosen=chosen, scope="decision record location", asked_event_id=asked,
        idempotency_key="pick-retry",
    )


def test_identical_picker_retry_returns_the_recorded_pair(ledger, ticket) -> None:
    asked = _ask_store(ledger)
    first = _pick(ledger, asked)
    before = len(ledger.events())
    again = _pick(ledger, asked)
    assert [event.id for event in again] == [event.id for event in first]
    assert len(ledger.events()) == before


def test_conflicting_picker_retry_is_refused(ledger, ticket) -> None:
    asked = _ask_store(ledger)
    first = _pick(ledger, asked)
    before = len(ledger.events())
    with pytest.raises(IdempotencyConflict):
        _pick(ledger, asked, chosen="Markdown in the shared repo")
    assert len(ledger.events()) == before
    assert ledger.event_by_key("pick-retry:election").id == first[1].id


def test_picker_writes_both_events_or_neither(ledger, ticket, monkeypatch) -> None:
    import ledger.verbs.pick as pick

    def refuse(*_args, **_kwargs):
        raise CliError("refused after the utterance", code="refused", exit_code=3)

    monkeypatch.setattr(pick, "record_election", refuse)
    before = len(ledger.events())
    with pytest.raises(CliError, match="refused after the utterance"):
        _pick(ledger, _ask_store(ledger))
    assert len(ledger.events()) == before + 1, "only the ask should remain"
    assert ledger.event_by_key("pick-retry:utterance") is None


def test_cli_pick_records_utterance_and_election(ledger, ticket, monkeypatch) -> None:
    db = str(ledger.path)
    argv = [
        "--json", "--db", db, "pick", ITEM, "--question", "Ship it?",
        "--option", "Yes::ship today", "--option", "No", "--chosen", "Yes",
        "--scope", "release",
    ]
    code, out, err = invoke(monkeypatch, argv)
    assert code == 3 and json.loads(out)["error"]["code"] == "db_fd_unknown"
    ledger.close()
    code, out, err = invoke(monkeypatch, argv)
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["utterance"]["channel"] == TOOL_ANSWER
    assert data["election"]["payload"]["quote_ref"] == data["utterance"]["id"]
    assert data["election"]["payload"]["rejected"] == ["No"]


# --- read verbs ---------------------------------------------------------------


def test_cli_decisions_and_export_item(ledger, ticket, monkeypatch) -> None:
    db = str(ledger.path)
    argv = ["--json", "--db", db, "decisions",
            "--tag", "team:TEAM-A", "--standing", "superseded"]
    code, out, err = invoke(monkeypatch, argv)
    assert code == 3 and json.loads(out)["error"]["code"] == "db_fd_unknown"
    _close(ledger)
    ledger.close()
    code, out, err = invoke(monkeypatch, argv)
    assert code == 0, err
    rows = json.loads(out)["data"]["decisions"]
    assert [row["decision_event_id"] for row in rows] == [ticket["naming"]]
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "export-item", ITEM])
    assert code == 0, err
    kinds = {record["kind"] for record in json.loads(out)["data"]["events"]}
    assert "item.progress" not in kinds
    assert "decision.recorded" in kinds


# --- the work and home ledgers stay separate --------------------------------


def test_work_and_home_resolve_to_separate_databases(tmp_path: Path) -> None:
    paths = resolve_system_paths({
        "work": tmp_path / "work" / "ledger.db",
        "home": tmp_path / "home" / "ledger.db",
    })
    assert paths["work"] != paths["home"]
    with open_ledger(paths["work"]) as work:
        work.append(kind="item.created", payload={"type": "ticket", "title": "work only"},
                    item_id="W-1", idempotency_key="w1")
    with open_ledger(paths["home"]) as home:
        assert home.events() == []


def test_two_systems_on_one_database_are_refused(tmp_path: Path, monkeypatch) -> None:
    shared = tmp_path / "shared" / "ledger.db"
    shared.parent.mkdir()
    with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
        resolve_system_paths({"work": shared, "home": shared})
    link_dir = tmp_path / "linked"
    os.symlink(shared.parent, link_dir)
    with pytest.raises(SharedDatabaseError):
        resolve_system_paths({"work": shared, "home": link_dir / "ledger.db"})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SharedDatabaseError):
        resolve_system_paths({"work": shared, "home": "shared/../shared/ledger.db"})


def _claims(db: Path) -> list[dict[str, Any]]:
    with open_ledger(db, create=False) as lg:
        return [event.payload for event in lg.events(kind="ledger.system")]


def test_hard_link_to_a_claimed_database_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "work" / "ledger.db"
    claim_database(db, "work")
    with open_ledger(db) as lg:
        lg.append(kind="item.created", payload={"type": "ticket", "title": "work only"},
                  item_id="W-1", idempotency_key="w1")
    linked = tmp_path / "home" / "ledger.db"
    linked.parent.mkdir()
    os.link(db, linked)
    with pytest.raises(UnsafeDatabaseFile, match=r"^db_hardlinked:"):
        claim_database(linked, "home")
    linked.unlink()
    assert _claims(db) == [{"system": "work"}]


def test_database_named_like_a_stamp_is_its_own_database(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    claim_database(db, "work")
    claim_database(tmp_path / "ledger.db.system", "home")
    assert _claims(db) == [{"system": "work"}]
    assert _claims(tmp_path / "ledger.db.system") == [{"system": "home"}]
    claim_database(db, "work")


def test_unclaimed_database_is_claimed_by_the_first_system(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        lg.append(kind="item.created", payload={"type": "ticket", "title": "legacy"},
                  item_id="L-1", idempotency_key="l1")
        assert lg.system() is None
    claim_database(db, "home")
    with pytest.raises(SharedDatabaseError, match="'home' and 'work' share one database"):
        claim_database(db, "work")
    with open_ledger(db) as lg:
        assert lg.system() == "home"
        assert [event.kind for event in lg.events()] == ["item.created", "ledger.system"]


def test_same_system_reclaims_without_a_second_claim(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    claim_database(db, "work")
    claim_database(db, "work")
    assert _claims(db) == [{"system": "work"}]


def test_interrupted_claim_leaves_the_database_unclaimed(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    original = Ledger._append

    def crash_after_write(self, **kwargs):
        original(self, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(Ledger, "_append", crash_after_write)
    with pytest.raises(KeyboardInterrupt):
        claim_database(db, "work")
    monkeypatch.undo()
    assert _claims(db) == []
    claim_database(db, "home")
    assert _claims(db) == [{"system": "home"}]


def test_system_claim_is_not_a_public_append(tmp_path: Path) -> None:
    with open_ledger(tmp_path / "ledger.db") as lg:
        with pytest.raises(ValidationError, match="reserved-kind:ledger.system"):
            lg.append(kind="ledger.system", payload={"system": "home"},
                      idempotency_key="forged")


def test_restored_export_keeps_its_claim(tmp_path: Path) -> None:
    db = tmp_path / "work" / "ledger.db"
    claim_database(db, "work")
    with open_ledger(db) as lg:
        lg.append(kind="item.created", payload={"type": "ticket", "title": "work only"},
                  item_id="W-1", idempotency_key="w1")
        export_events(lg, tmp_path / "export")
    restored = tmp_path / "restored" / "ledger.db"
    restore_export(tmp_path / "export", restored, system="work")
    assert _claims(restored) == [{"system": "work"}]
    with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
        claim_database(restored, "home")


# --- one system claim per database, at write, restore and verify -------------
#
# The claim is a ledger.system event on the hash chain. A database with no
# claim on record is bound by the first claim written to it, on the write
# path and on a restore for a named system, as before claims were enforced.


def _forge_claim(lg: Ledger, system: str, key: str) -> Event:
    """Write a claim on the internal write path, past claim_system's own read
    of the current owner, so only the shared write guard stands in the way."""
    return lg._append(kind="ledger.system", payload={"system": system},
                      idempotency_key=key, internal=True,
                      ignore_retry_attribution=False)


def test_write_guard_refuses_another_systems_claim(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    claim_database(db, "work")
    with open_ledger(db) as lg:
        with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
            _forge_claim(lg, "home", "forged-home")
    assert _claims(db) == [{"system": "work"}]


def test_write_guard_refuses_a_repeated_claim(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    claim_database(db, "work")
    with open_ledger(db) as lg:
        with pytest.raises(ValidationError, match="system-already-claimed:work"):
            _forge_claim(lg, "work", "forged-work")
    assert _claims(db) == [{"system": "work"}]


def test_write_guard_lets_the_first_claim_bind_an_unclaimed_database(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        lg.append(kind="item.created", payload={"type": "ticket", "title": "legacy"},
                  item_id="L-1", idempotency_key="l1")
        _forge_claim(lg, "home", "first-claim")
        assert lg.system() == "home"


def test_loop_writer_cannot_write_a_system_claim(tmp_path: Path) -> None:
    with open_ledger(tmp_path / "ledger.db") as lg:
        with pytest.raises(ValidationError, match="reserved-kind:ledger.system"):
            lg._append_loop_event(_LOOP_WRITER_CAPABILITY, kind="ledger.system",
                                  payload={"system": "home"}, idempotency_key="loop-claim")
        assert lg.system() is None


def _claim_chain(*systems: str) -> list[Event]:
    """A hash-chained stream: one item, then one claim per ``systems`` entry."""
    specs: list[tuple[str, dict[str, Any], str | None]] = [
        ("item.created", {"type": "ticket", "title": "Claimed"}, ITEM),
    ]
    specs += [("ledger.system", {"system": system}, None) for system in systems]
    events: list[Event] = []
    prev = GENESIS_HASH
    for index, (kind, payload, item_id) in enumerate(specs, start=1):
        draft = Event(id=index, kind=kind, payload_version=1, payload=payload,
                      item_id=item_id, idempotency_key=f"claim-{index}", actor=None,
                      harness="pytest", model=None, session=None, channel=None,
                      provenance_class=None, recorded_at="2026-01-01T00:00:00.000Z",
                      prev_hash=prev, hash="")
        event = replace(draft, hash=event_digest(hash_body(draft)))
        events.append(event)
        prev = event.hash
    return events


def _claim_export(export_dir: Path, *systems: str) -> None:
    events = _claim_chain(*systems)
    write_event_files(export_dir, events)
    write_watermark(export_dir, events[-1].id)


def test_verify_export_refuses_two_claims(tmp_path: Path) -> None:
    _claim_export(tmp_path / "export", "work", "home")
    with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
        verify_export(tmp_path / "export")


def test_restore_refuses_an_export_with_two_claims(tmp_path: Path) -> None:
    _claim_export(tmp_path / "export", "work", "home")
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
        restore_export(tmp_path / "export", dest, system="work")
    assert not any(dest.parent.glob("ledger.db*")), "a refused restore left files behind"


def test_replay_guard_refuses_a_second_claim(tmp_path: Path) -> None:
    # Replay without the export check in front of it: the per-row guard alone.
    with open_ledger(tmp_path / "ledger.db") as lg:
        with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
            replay_events(lg, _claim_chain("work", "home"))
        assert lg.events() == []
        with pytest.raises(LedgerError,
                           match="restore refuses event 3 .*system-already-claimed:work"):
            replay_events(lg, _claim_chain("work", "work"))
        assert lg.events() == []


def test_verify_refuses_a_database_with_two_claims(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        replay_events(lg, _claim_chain("work"))
        lg.verify()
    forged = _claim_chain("work", "home")[-1]
    raw = sqlite3.connect(str(db), isolation_level=None)
    try:
        raw.execute(
            "INSERT INTO events (id, kind, payload_version, payload, item_id, idempotency_key,"
            " actor, harness, model, session, channel, provenance_class, recorded_at,"
            " prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (forged.id, forged.kind, 1, json.dumps(forged.payload), None,
             forged.idempotency_key, None, "pytest", None, None, None, None,
             forged.recorded_at, forged.prev_hash, forged.hash),
        )
    finally:
        raw.close()
    with open_ledger(db) as lg:
        with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
            lg.verify()


def test_restore_for_another_system_is_refused(tmp_path: Path) -> None:
    _claim_export(tmp_path / "export", "work")
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
        restore_export(tmp_path / "export", dest, system="home")
    assert not dest.parent.exists()
    result = restore_export(tmp_path / "export", dest, system="work")
    assert result.events == 2
    assert _claims(dest) == [{"system": "work"}]


def test_restore_of_an_unclaimed_export_claims_it_for_the_named_system(tmp_path: Path) -> None:
    _claim_export(tmp_path / "export")
    named = restore_export(tmp_path / "export", tmp_path / "named" / "ledger.db",
                           system="home")
    assert named.events == 2
    assert _claims(tmp_path / "named" / "ledger.db") == [{"system": "home"}]


def test_restore_without_a_system_is_refused(tmp_path: Path) -> None:
    # No target means no target check, so the restore refuses rather than
    # replaying an export it cannot place, claimed or not.
    _claim_export(tmp_path / "claimed", "work")
    _claim_export(tmp_path / "unclaimed")
    for export in ("claimed", "unclaimed"):
        dest = tmp_path / f"restored-{export}" / "ledger.db"
        with pytest.raises(ValidationError, match="restore-needs-system"):
            restore_export(tmp_path / export, dest, system=None)
        assert not dest.parent.exists()


# --- verification against the caller's system ----------------------------------


def test_verify_refuses_a_lone_claim_for_another_system(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    claim_database(db, "work")
    with open_ledger(db) as lg:
        lg.verify()
        lg.verify(system="work")
        with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
            lg.verify(system="home")


def test_verify_for_a_system_refuses_an_unclaimed_database(tmp_path: Path) -> None:
    with open_ledger(tmp_path / "ledger.db") as lg:
        lg.append(kind="item.created", payload={"type": "ticket", "title": "legacy"},
                  item_id="L-1", idempotency_key="l1")
        lg.verify()
        with pytest.raises(ValidationError, match="system-unclaimed:home"):
            lg.verify(system="home")


def test_verify_export_refuses_a_lone_claim_for_another_system(tmp_path: Path) -> None:
    _claim_export(tmp_path / "export", "work")
    assert len(verify_export(tmp_path / "export")) == 2
    assert len(verify_export(tmp_path / "export", system="work")) == 2
    with pytest.raises(SharedDatabaseError, match="'work' and 'home' share one database"):
        verify_export(tmp_path / "export", system="home")


def test_verify_export_for_a_system_refuses_an_unclaimed_export(tmp_path: Path) -> None:
    _claim_export(tmp_path / "export")
    assert len(verify_export(tmp_path / "export")) == 1
    with pytest.raises(ValidationError, match="system-unclaimed:home"):
        verify_export(tmp_path / "export", system="home")


def test_cli_restore_and_verify_take_the_target_system(tmp_path: Path, monkeypatch) -> None:
    _claim_export(tmp_path / "export", "work")
    dest = tmp_path / "restored" / "ledger.db"
    code, _out, err = invoke(monkeypatch, ["--db", str(dest), "restore",
                                           "--dir", str(tmp_path / "export")])
    assert code == 2 and "pass --system" in err
    code, out, _err = invoke(monkeypatch, ["--json", "--db", str(dest), "restore",
                                           "--dir", str(tmp_path / "export"),
                                           "--system", "home"])
    assert code == 3
    assert json.loads(out)["error"]["code"] == "shared_database"
    assert not dest.parent.exists()
    code, _out, err = invoke(monkeypatch, ["--db", str(dest), "restore",
                                           "--dir", str(tmp_path / "export"),
                                           "--system", "work"])
    assert code == 0, err
    code, out, _err = invoke(monkeypatch, ["--json", "--db", str(dest), "verify",
                                           "--system", "home"])
    assert code == 3
    assert json.loads(out)["error"]["code"] == "shared_database"
    code, out, err = invoke(monkeypatch, ["--db", str(dest), "verify", "--system", "work"])
    assert code == 0, err
    assert out == "ok events=2\n"


def test_busy_claim_checkpoint_is_reported(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        lg.append(kind="item.created", payload={"type": "ticket", "title": "t"},
                  item_id="W-1", idempotency_key="w1")
    # A reader in another process holding a transaction keeps the
    # checkpoint from truncating the WAL without sharing SQLite descriptors.
    reader = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("BEGIN")
conn.execute("SELECT count(*) FROM events").fetchone()
print("held", flush=True)
sys.stdin.readline()
conn.close()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", reader, str(db)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    try:
        with pytest.raises(SharedDatabaseError, match=r"checkpoint of .* is busy"):
            claim_database(db, "work")
    finally:
        assert proc.stdin is not None
        proc.stdin.write("\n")
        proc.stdin.flush()
        _out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
    assert _claims(db) == [{"system": "work"}]


def test_claim_through_a_hard_link_with_unsaved_frames_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "work" / "ledger.db"
    claim_database(db, "work")
    linked = tmp_path / "home" / "ledger.db"
    linked.parent.mkdir()
    with open_ledger(db) as holder:
        holder.append(kind="item.created", payload={"type": "ticket", "title": "pending"},
                      item_id="W-1", idempotency_key="w1")
        os.link(db, linked)
        with pytest.raises(UnsafeDatabaseFile, match=r"^db_hardlinked:"):
            claim_database(linked, "home")


def test_claim_names_a_failure_that_is_not_a_shared_database(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        lg.append(kind="item.created", payload={"type": "ticket", "title": "t"},
                  item_id="W-1", idempotency_key="w1")
    db.chmod(0o444)
    try:
        with pytest.raises(LedgerError, match="could not claim .*readonly database") as caught:
            claim_database(db, "work")
        assert not isinstance(caught.value, SharedDatabaseError)
    finally:
        db.chmod(0o644)
    assert _claims(db) == []
