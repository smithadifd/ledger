"""Entity registry (LC8): automations, projects, machines and models as ledger state.

``ledger entity register`` and ``entity set`` are the only writers of
``entity.registered`` and ``entity.state``; ``ledger entities`` renders
``v_entities`` (migration 0009) as the drift-check JSON list or as one
markdown table per type under a line naming the ledger revision.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ledger import ValidationError, loop, store
from ledger.schema import ENTITY_STATE_FIELDS, ENTITY_TYPES, validate_payload
from ledger.store import open_ledger
from ledger.verbs import entity as entity_verb
from ledger.verbs.entities import ENTITY_KEYS
from ledger.verbs.entity import register_entity, set_entity_state

from test_cli import invoke, seed

JOB = "com.example.synthetic-brief"
TITLE = "Synthetic morning brief"
HEADER = (
    "Rendered by `ledger entities --format md` from ledger revision {revision} "
    "(the id of the newest event). Change the ledger, not this file."
)


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def cli(monkeypatch, db: str, *argv: str, env=None):
    return invoke(monkeypatch, ["--db", db, *argv], env=env)


def cli_json(monkeypatch, db: str, *argv: str, env=None):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, *argv], env=env)
    return code, (json.loads(out) if out.strip() else None), err


def register(monkeypatch, db: str, entity_id: str = JOB, title: str = TITLE,
             entity_type: str = "automation"):
    return cli_json(monkeypatch, db, "entity", "register", entity_type, entity_id,
                    "--title", title)


def set_state(monkeypatch, db: str, *flags: str, entity_id: str = JOB,
              entity_type: str = "automation"):
    return cli_json(monkeypatch, db, "entity", "set", entity_type, entity_id, *flags)


def entity_events(db: str) -> list:
    with open_ledger(db, create=False) as lg:
        return [event for event in lg.events() if event.kind.startswith("entity.")]


def newest_event_id(db: str) -> int:
    with open_ledger(db, create=False) as lg:
        return lg.events()[-1].id


def test_register_then_a_schedule_change_renders_the_new_value_and_revision(
    db, monkeypatch
) -> None:
    code, _body, err = register(monkeypatch, db)
    assert code == 0, err
    code, _body, err = set_state(
        monkeypatch, db, "--schedule", "daily 06:30", "--wrapper", "bin/synthetic-brief.sh",
        "--host", "studio", "--risk-class", "A", "--status", "active",
    )
    assert code == 0, err
    code, moved, err = set_state(monkeypatch, db, "--schedule", "daily 07:15")
    assert code == 0, err
    assert moved["data"]["changed"] == ["schedule"], moved
    newest = moved["data"]["event"]["id"]

    code, out, err = cli(monkeypatch, db, "entities", "--format", "md")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == HEADER.format(revision=newest), lines[0]
    assert "## automation" in lines
    header = lines[lines.index("## automation") + 2]
    assert header == "| id | title | schedule | wrapper | host | risk_class | status | revision |"
    row = next(line for line in lines if line.startswith(f"| {JOB} |"))
    assert "daily 07:15" in row, f"the render must show the newest schedule: {row}"
    assert "daily 06:30" not in out, f"the render kept a superseded schedule: {row}"
    assert row.endswith(f"| {newest} |"), row

    code, out, err = cli(monkeypatch, db, "entities", "--format", "json")
    assert code == 0, err
    [entity] = json.loads(out)
    assert entity["state"]["schedule"] == "daily 07:15", entity
    assert entity["revision"] == newest
    assert entity["state"] == {
        "schedule": "daily 07:15", "wrapper": "bin/synthetic-brief.sh", "host": "studio",
        "risk_class": "A", "status": "active",
    }


def test_md_render_names_the_ledger_revision_on_its_first_line(db, monkeypatch) -> None:
    assert register(monkeypatch, db)[0] == 0
    # A later, unrelated event moves the ledger revision past the entity's own.
    seed(monkeypatch, db, "it-1")
    store_revision = newest_event_id(db)
    code, out, err = cli(monkeypatch, db, "entities")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == HEADER.format(revision=store_revision), (
        f"the md render must open with the ledger revision line: {lines[:2]}"
    )
    assert [line for line in lines if line.startswith("## ")] == [
        f"## {kind}" for kind in ENTITY_TYPES
    ]
    code, out, err = cli(monkeypatch, db, "entities", "--type", "project")
    assert code == 0, err
    assert out.splitlines() == [
        HEADER.format(revision=store_revision), "", "## project", "",
        "| id | title | revision |", "|---|---|---|",
    ]


def test_an_empty_ledger_renders_revision_zero(db, monkeypatch) -> None:
    open_ledger(db).close()
    code, out, err = cli(monkeypatch, db, "entities", "--format", "md", "--type", "model")
    assert code == 0, err
    assert out.splitlines()[0] == HEADER.format(revision=0)
    code, out, err = cli(monkeypatch, db, "entities", "--format", "json")
    assert code == 0, err
    assert json.loads(out) == []


def test_setting_a_field_to_its_current_value_appends_nothing(db, monkeypatch) -> None:
    assert register(monkeypatch, db)[0] == 0
    code, first, err = set_state(monkeypatch, db, "--schedule", "daily 06:30")
    assert code == 0, err
    assert first["data"]["unchanged"] is False
    before = entity_events(db)

    code, again, err = set_state(monkeypatch, db, "--schedule", "daily 06:30")
    assert code == 0, err
    assert entity_events(db) == before, "a same-value state event must append nothing"
    assert again["data"]["unchanged"] is True, again
    assert again["data"]["event"] is None
    assert again["data"]["revision"] == first["data"]["event"]["id"]

    code, out, err = cli(monkeypatch, db, "entity", "set", "automation", JOB,
                         "--schedule", "daily 06:30")
    assert code == 0, err
    assert out == f"entity\tautomation/{JOB}\tunchanged\n"

    code, mixed, err = set_state(monkeypatch, db, "--schedule", "daily 06:30",
                                 "--host", "studio")
    assert code == 0, err
    assert mixed["data"]["changed"] == ["host"]
    assert mixed["data"]["event"]["payload"] == {
        "type": "automation", "id": JOB, "state": {"host": "studio"},
    }, "a state event carries only the fields that changed"


def test_registering_an_existing_key_refuses(db, monkeypatch) -> None:
    code, first, err = register(monkeypatch, db)
    assert code == 0, err
    before = entity_events(db)
    for title in (TITLE, "Another synthetic title"):
        code, body, _err = register(monkeypatch, db, title=title)
        assert code == 3, f"re-registering {JOB} must refuse (rc={code}): {body}"
        assert body["error"]["code"] == "entity_exists", body
        assert f"automation/{JOB}" in body["error"]["message"]
    assert entity_events(db) == before, "a refused registration must append nothing"
    with open_ledger(db) as lg:
        with pytest.raises(entity_verb.CliError, match="entity already registered"):
            register_entity(lg, "automation", JOB, TITLE, harness="pytest")
    # The key is (type, id): a project may share the automation's id.
    code, body, err = register(monkeypatch, db, entity_type="project")
    assert code == 0, err
    assert body["data"]["event"]["id"] != first["data"]["event"]["id"]


def test_an_unknown_state_field_is_refused_and_not_stored(db, monkeypatch) -> None:
    assert register(monkeypatch, db)[0] == 0
    with open_ledger(db) as lg:
        with pytest.raises(ValidationError, match=r"unknown-key:state\.colour"):
            set_entity_state(lg, "automation", JOB,
                             {"schedule": "daily 06:30", "colour": "red"}, harness="pytest")
        with pytest.raises(ValidationError, match=r"bad-type:state"):
            set_entity_state(lg, "automation", JOB, {}, harness="pytest")
    assert [event.kind for event in entity_events(db)] == ["entity.registered"], (
        "a refused state event must store nothing"
    )
    with pytest.raises(ValidationError, match=r"unknown-key:state\.colour"):
        validate_payload("entity.state",
                         {"type": "automation", "id": JOB, "state": {"colour": "red"}})
    with pytest.raises(ValidationError, match=r"unknown-key:owner"):
        validate_payload("entity.registered",
                         {"type": "automation", "id": JOB, "title": TITLE, "owner": "x"})


@pytest.mark.parametrize(
    "kind,payload,reason",
    [
        ("entity.registered", {"type": "robot", "id": JOB, "title": TITLE}, "bad-type:type"),
        ("entity.registered", {"type": "automation", "id": "has space", "title": TITLE},
         "bad-type:id"),
        ("entity.registered", {"type": "automation", "id": JOB, "title": "two\nlines"},
         "not-single-line:title"),
        ("entity.state", {"type": "automation", "id": JOB, "state": {"host": "two words"}},
         "bad-type:state.host"),
        ("entity.state", {"type": "automation", "id": JOB, "state": {"wrapper": "a b"}},
         "bad-type:state.wrapper"),
        ("entity.state", {"type": "automation", "id": JOB, "state": {"schedule": "a\nb"}},
         "not-single-line:state.schedule"),
        ("entity.state", {"type": "automation", "id": JOB, "state": {"status": ""}},
         "bad-type:state.status"),
    ],
)
def test_entity_payloads_are_closed(kind, payload, reason) -> None:
    with pytest.raises(ValidationError) as caught:
        validate_payload(kind, payload)
    assert caught.value.reason == reason


def test_entity_events_carry_no_item_id() -> None:
    from ledger.schema import validate_item_id

    for kind in ("entity.registered", "entity.state"):
        assert validate_item_id(kind, None) is None
        with pytest.raises(ValidationError, match="unexpected-key:item_id"):
            validate_item_id(kind, "it-1")


def test_state_for_an_unregistered_entity_is_not_found(db, monkeypatch) -> None:
    open_ledger(db).close()
    code, body, _err = set_state(monkeypatch, db, "--status", "active")
    assert code == 1, body
    assert body["error"]["code"] == "not_found"
    assert entity_events(db) == []


def test_cli_usage_refusals(db, monkeypatch) -> None:
    assert register(monkeypatch, db)[0] == 0
    cases = [
        (("entity", "register", "automation", "other"), "needs --title"),
        (("entity", "register", "automation", "other", "--title", "T", "--host", "studio"),
         "does not take --host"),
        (("entity", "set", "automation", JOB), "needs at least one of"),
        (("entity", "set", "automation", JOB, "--title", "T", "--host", "h"),
         "does not take --title"),
        (("entity", "register", "robot", "x", "--title", "T"), "invalid choice"),
    ]
    for argv, message in cases:
        code, out, err = cli(monkeypatch, db, *argv)
        assert code == 2, (argv, out, err)
        assert message in err, (argv, err)


def _forged(kind: str) -> dict:
    if kind == "entity.registered":
        return {"type": "automation", "id": "forged-job", "title": "Forged"}
    return {"type": "automation", "id": JOB, "state": {"schedule": "forged"}}


@pytest.mark.parametrize("kind", ["entity.registered", "entity.state"])
def test_generic_writers_cannot_forge_entity_events(db, monkeypatch, kind) -> None:
    assert register(monkeypatch, db)[0] == 0
    seed(monkeypatch, db, "it-1")
    before = entity_events(db)
    code, _out, err = cli(monkeypatch, db, "append", "--kind", kind,
                          "--payload", json.dumps(_forged(kind)))
    assert code == 3, f"generic append must refuse {kind} (rc={code}): {err}"
    assert f"reserved-kind:{kind}" in err
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        [{"kind": "item.note", "payload": {"text": "same batch"}},
         {"kind": kind, "payload": _forged(kind)}]
    )))
    code, _out, err = cli(monkeypatch, db, "append-many", "it-1")
    assert code == 3, f"append-many must refuse {kind} (rc={code}): {err}"
    assert f"reserved-kind:{kind}" in err
    with open_ledger(db) as lg:
        with pytest.raises(ValidationError, match=f"reserved-kind:{kind}"):
            lg.append(kind=kind, payload=_forged(kind), idempotency_key="forged-1",
                      harness="pytest")
        with pytest.raises(ValidationError, match=f"reserved-kind:{kind}"):
            lg._append_loop_event(loop._LOOP_WRITER_CAPABILITY, kind=kind,
                                  payload=_forged(kind), idempotency_key="forged-2",
                                  harness="pytest")
        with pytest.raises(ValidationError, match="entity-writer-capability-required"):
            lg._append_entity_event(object(), kind=kind, payload=_forged(kind),
                                    idempotency_key="forged-3", harness="pytest")
        with pytest.raises(ValidationError, match="reserved-kind:item.note"):
            lg._append_entity_event(entity_verb._ENTITY_WRITER_CAPABILITY, kind="item.note",
                                    item_id="it-1", payload={"text": "x"},
                                    idempotency_key="forged-4", harness="pytest")
        with pytest.raises(ValidationError, match="unexpected-key:item_id"):
            lg._append_entity_event(entity_verb._ENTITY_WRITER_CAPABILITY, kind=kind,
                                    item_id="it-1", payload=_forged(kind),
                                    idempotency_key="forged-5", harness="pytest")
    assert entity_events(db) == before, f"no writer but the entity verb may add {kind}"


def test_json_contract_order_and_type_filter(db, monkeypatch) -> None:
    for entity_type, entity_id in (("project", "zeta"), ("automation", "b-job"),
                                   ("model", "m-1"), ("automation", "a-job"),
                                   ("machine", "studio")):
        assert register(monkeypatch, db, entity_id=entity_id, entity_type=entity_type,
                        title=f"Title {entity_id}")[0] == 0
    assert set_state(monkeypatch, db, "--status", "active", "--host", "studio",
                     "--schedule", "weekly", entity_id="a-job")[0] == 0
    code, out, err = cli(monkeypatch, db, "entities", "--format", "json")
    assert code == 0, err
    entities = json.loads(out)
    assert [(e["type"], e["id"]) for e in entities] == [
        ("automation", "a-job"), ("automation", "b-job"), ("project", "zeta"),
        ("machine", "studio"), ("model", "m-1"),
    ]
    for entity in entities:
        assert tuple(entity) == ENTITY_KEYS, entity
    assert list(entities[0]["state"]) == ["schedule", "host", "status"]
    assert entities[1]["state"] == {}

    code, out, err = cli(monkeypatch, db, "entities", "--type", "automation",
                         "--format", "json")
    assert code == 0, err
    assert [e["id"] for e in json.loads(out)] == ["a-job", "b-job"]

    code, envelope, err = cli_json(monkeypatch, db, "entities", "--type", "automation")
    assert code == 0, err
    assert envelope["data"] == json.loads(out)
    code, envelope, _err = cli_json(monkeypatch, db, "entities", "--format", "md")
    assert code == 2 and envelope["error"]["code"] == "usage", envelope


def test_entities_reads_without_a_harness_and_entity_needs_one(db, monkeypatch) -> None:
    assert register(monkeypatch, db)[0] == 0
    code, out, err = cli(monkeypatch, db, "entities", "--format", "json",
                         env={"LEDGER_HARNESS": None})
    assert code == 0, err
    assert [e["id"] for e in json.loads(out)] == [JOB]
    code, body, _err = cli_json(monkeypatch, db, "entity", "set", "automation", JOB,
                                "--status", "paused", env={"LEDGER_HARNESS": None})
    assert code == 3 and body["error"]["code"] == "missing_harness", body


def test_md_cells_escape_pipes(db, monkeypatch) -> None:
    assert register(monkeypatch, db, title="Brief | digest")[0] == 0
    assert set_state(monkeypatch, db, "--schedule", "mon|thu 06:00")[0] == 0
    code, out, err = cli(monkeypatch, db, "entities", "--type", "automation")
    assert code == 0, err
    row = next(line for line in out.splitlines() if line.startswith(f"| {JOB} |"))
    assert row == f"| {JOB} | Brief \\| digest | mon\\|thu 06:00 | {newest_event_id(db)} |"


def test_entity_events_verify_and_carry_the_writer_identity(db, monkeypatch) -> None:
    assert register(monkeypatch, db)[0] == 0
    assert set_state(monkeypatch, db, "--host", "studio")[0] == 0
    registered, state = entity_events(db)
    assert registered.item_id is None and state.item_id is None
    assert registered.idempotency_key == f"entity:automation:{JOB}:registered"
    assert state.idempotency_key == (
        f"entity:automation:{JOB}:state:after:{registered.id}"
    )
    assert (state.harness, state.actor, state.session) == ("pytest", "tester", "sess-1")
    with open_ledger(db) as lg:
        lg.verify()


def test_a_ledger_at_0008_migrates_and_keeps_every_older_view(tmp_path) -> None:
    path = tmp_path / "old.db"
    migrations = store.load_migrations()
    assert "0009_entities.sql" in [m.name for m in migrations]
    conn, _proof = store.connect(path)
    try:
        store.migrate(conn, [m for m in migrations if m.version <= 8])
        before = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('view', 'table', 'trigger')"
        ).fetchall())
    finally:
        conn.close()
    with open_ledger(path) as lg:
        after = dict(lg._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('view', 'table', 'trigger')"
        ).fetchall())
        columns = [row[1] for row in lg._conn.execute("PRAGMA table_info(v_entities)")]
    assert set(after) - set(before) == {"v_entities"}
    for name, sql in before.items():
        if name == "v_items":
            continue  # 0010 adds its columns
        assert after[name] == sql, f"0009 changed {name}"
    assert columns == ["type", "entity_id", "title", "state", "registered_event_id",
                       "revision", "registered_at", "updated_at"]


def test_state_fields_are_the_schema_order() -> None:
    assert ENTITY_STATE_FIELDS == ("schedule", "wrapper", "host", "risk_class", "status")
    assert ENTITY_TYPES == ("automation", "project", "machine", "model")
