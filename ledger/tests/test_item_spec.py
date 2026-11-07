"""Item spec versions (LC3): a ticket's content is an immutable, versioned event.

``ledger spec set`` appends ``item.spec`` version N+1 only when the content
hash differs; ``spec show`` and ``spec diff`` read the versions back. A claim
(``ledger claim`` or the loop's take) stamps the version it builds against,
and the card, ``pick`` and the work loop print ``spec moved: vN → vM`` when
the current spec is newer than the claim's. Only ``set_spec`` writes
``item.spec``: generic ``append`` and ``append-many`` refuse the kind, and a
merge class is one of MC-0 to MC-4.
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from ledger import ValidationError, loop, store
from ledger.card import claim_spec_version, item_spec_moved, project_card
from ledger.store import canonical_dumps, open_ledger
from ledger.verbs import spec as spec_verb
from ledger.verbs.spec import content_hash, set_spec

from test_cli import invoke, seed

ITEM = "it-1"
BODY_ONE = "Build the thing.\n"
BODY_TWO = "Build the thing.\nThen test it.\n"
FORGED = {"version": 1, "title": "Forged", "body_md": "forged body",
          "content_hash": "0" * 64}


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    seed(monkeypatch, path, ITEM)
    return path


def cli(monkeypatch, db: str, *argv: str, env=None):
    return invoke(monkeypatch, ["--db", db, *argv], env=env)


def cli_json(monkeypatch, db: str, *argv: str, env=None):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, *argv], env=env)
    return code, (json.loads(out) if out.strip() else None), err


def set_body(monkeypatch, db: str, body: str, *extra: str):
    return cli_json(
        monkeypatch, db, "spec", "set", ITEM, "--title", "Spec title", "--body", body, *extra
    )


def spec_events(db: str) -> list:
    with open_ledger(db, create=False) as lg:
        return lg.events(item_id=ITEM, kind="item.spec")


def card_text(monkeypatch, db: str) -> str:
    code, out, err = cli(monkeypatch, db, "card", ITEM)
    assert code == 0, err
    return out


def test_set_same_spec_twice_is_one_event(db, monkeypatch) -> None:
    code, first, err = set_body(monkeypatch, db, BODY_ONE, "--size", "M")
    assert code == 0, err
    assert first["data"]["version"] == 1 and first["data"]["unchanged"] is False
    code, second, err = set_body(monkeypatch, db, BODY_ONE, "--size", "M")
    assert code == 0, err
    assert second["data"]["unchanged"] is True, second
    assert second["data"]["version"] == 1
    assert second["data"]["event"]["id"] == first["data"]["event"]["id"]
    assert [event.payload["version"] for event in spec_events(db)] == [1]


def test_same_content_from_a_newer_commit_is_a_no_op(db, monkeypatch) -> None:
    code, _first, err = set_body(
        monkeypatch, db, BODY_ONE, "--source", "roadmaps/x.md", "--source-rev", "abc1234"
    )
    assert code == 0, err
    code, second, err = set_body(
        monkeypatch, db, BODY_ONE, "--source", "roadmaps/x.md", "--source-rev", "def5678"
    )
    assert code == 0, err
    assert second["data"]["unchanged"] is True, "a new source_rev alone must not version the spec"
    [event] = spec_events(db)
    assert event.payload["source_rev"] == "abc1234"


def _versions(db: str) -> list[tuple[int, str | None, str]]:
    return [
        (e.payload["version"], e.payload.get("source"), e.payload["content_hash"])
        for e in spec_events(db)
    ]


def test_a_source_only_change_writes_a_new_version(db, monkeypatch) -> None:
    code, v1, err = set_body(monkeypatch, db, BODY_ONE, "--source", "A")
    assert code == 0, err
    assert v1["data"]["version"] == 1
    code, v2, err = set_body(monkeypatch, db, BODY_ONE, "--source", "B")
    assert code == 0, err
    assert v2["data"]["unchanged"] is False and v2["data"]["version"] == 2, v2
    assert v2["data"]["event"]["payload"]["source"] == "B"
    with open_ledger(db, create=False) as lg:
        assert spec_verb.current_spec(lg, ITEM).payload["source"] == "B"
    rows = _versions(db)
    assert [(v, s) for v, s, _h in rows] == [(1, "A"), (2, "B")]
    assert rows[0][2] == rows[1][2], "a source move keeps the content hash"
    # The same call again is idempotent: nothing written, v2 returned.
    code, again, err = set_body(monkeypatch, db, BODY_ONE, "--source", "B")
    assert code == 0, err
    assert again["data"]["unchanged"] is True and again["data"]["version"] == 2
    assert again["data"]["event"]["id"] == v2["data"]["event"]["id"]
    assert len(spec_events(db)) == 2
    # A move back to an earlier source is a change.
    code, v3, err = set_body(monkeypatch, db, BODY_ONE, "--source", "A")
    assert code == 0, err
    assert v3["data"]["unchanged"] is False and v3["data"]["version"] == 3
    assert [(v, s) for v, s, _h in _versions(db)] == [(1, "A"), (2, "B"), (3, "A")]


def test_a_content_change_still_bumps_the_version_with_a_source(db, monkeypatch) -> None:
    set_body(monkeypatch, db, BODY_ONE, "--source", "A")
    code, v2, err = set_body(monkeypatch, db, BODY_TWO, "--source", "A")
    assert code == 0, err
    assert v2["data"]["unchanged"] is False and v2["data"]["version"] == 2
    code, v3, err = set_body(monkeypatch, db, BODY_ONE, "--source", "B")
    assert code == 0, err
    assert v3["data"]["version"] == 3
    assert [(v, s) for v, s, _h in _versions(db)] == [(1, "A"), (2, "A"), (3, "B")]


def test_an_absent_source_compares_content_only(db, monkeypatch) -> None:
    set_body(monkeypatch, db, BODY_ONE, "--source", "A")
    code, again, err = set_body(monkeypatch, db, BODY_ONE)
    assert code == 0, err
    assert again["data"]["unchanged"] is True and again["data"]["version"] == 1
    assert len(spec_events(db)) == 1


def test_a_retry_of_a_source_move_writes_once(db) -> None:
    content = {"title": "T", "body_md": "one"}
    with open_ledger(db, create=False) as lg:
        set_spec(lg, ITEM, content, source="A", harness="pytest")
        first, created = set_spec(lg, ITEM, content, source="B", harness="pytest")
        assert created is True and first.idempotency_key == f"spec:{ITEM}:v2"
        retry, created = set_spec(lg, ITEM, content, source="B", harness="pytest")
        assert created is False and retry.id == first.id
        assert [e.idempotency_key for e in spec_verb.spec_events(lg, ITEM)] == [
            f"spec:{ITEM}:v1", f"spec:{ITEM}:v2"
        ]


def test_content_hash_is_sha256_of_canonical_content_fields() -> None:
    content = {"title": "T", "body_md": "B", "size": "S"}
    expected = hashlib.sha256(
        canonical_dumps({
            "acceptance": None, "body_md": "B", "merge_class": None, "size": "S", "title": "T",
        }).encode("utf-8")
    ).hexdigest()
    assert content_hash(content) == expected
    assert content_hash({**content, "source_rev": "abc", "source": "x", "version": 9}) == expected


def test_changed_body_is_version_two_and_the_diff_names_the_field(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    code, second, err = set_body(monkeypatch, db, BODY_TWO)
    assert code == 0, err
    assert second["data"]["version"] == 2 and second["data"]["unchanged"] is False
    [one, two] = spec_events(db)
    assert one.payload["content_hash"] != two.payload["content_hash"]

    code, out, err = cli(monkeypatch, db, "spec", "diff", ITEM)
    assert code == 0, err
    assert out.splitlines()[0] == f"spec diff {ITEM} v1 → v2"
    assert "## body_md" in out.splitlines()
    assert "+Then test it." in out.splitlines()
    assert "## title" not in out

    code, body, err = cli_json(monkeypatch, db, "spec", "diff", ITEM, "v1", "v2")
    assert code == 0, err
    assert body["data"]["changed"] == ["body_md"]
    assert (body["data"]["from"], body["data"]["to"]) == (1, 2)


def test_diff_names_every_changed_field_in_order(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE, "--size", "S")[0] == 0
    code, _out, err = cli_json(
        monkeypatch, db, "spec", "set", ITEM, "--title", "New title", "--body", BODY_ONE,
        "--size", "L", "--merge-class", "MC-2",
    )
    assert code == 0, err
    code, body, err = cli_json(monkeypatch, db, "spec", "diff", ITEM, "1")
    assert code == 0, err
    assert body["data"]["changed"] == ["title", "size", "merge_class"]


def test_unknown_versions_refuse(db, monkeypatch) -> None:
    code, _out, err = cli(monkeypatch, db, "spec", "show", ITEM)
    assert code == 1 and f"item {ITEM} has no spec" in err
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    code, _out, err = cli(monkeypatch, db, "spec", "diff", ITEM)
    assert code == 1 and "no earlier version" in err
    code, _out, err = cli(monkeypatch, db, "spec", "show", ITEM, "--version", "3")
    assert code == 1 and f"item {ITEM} has no spec v3 (current is v1)" in err
    code, _out, err = cli(monkeypatch, db, "spec", "diff", ITEM, "v1", "v7")
    assert code == 1 and "has no spec v7" in err
    code, _out, err = cli(monkeypatch, db, "spec", "diff", ITEM, "seven")
    assert code == 2 and "spec version" in err


def test_show_prints_the_current_or_a_given_version(db, monkeypatch, tmp_path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY_ONE, encoding="utf-8")
    code, _out, err = cli_json(
        monkeypatch, db, "spec", "set", ITEM, "--title", "Spec title",
        "--body-file", str(body_file), "--acceptance", "It is built.",
    )
    assert code == 0, err
    assert set_body(monkeypatch, db, BODY_TWO)[0] == 0
    code, out, err = cli(monkeypatch, db, "spec", "show", ITEM)
    assert code == 0, err
    assert out.startswith(f"spec v2\t{ITEM}\t")
    assert "Then test it." in out
    code, body, err = cli_json(monkeypatch, db, "spec", "show", ITEM, "--version", "v1")
    assert code == 0, err
    assert body["data"]["version"] == 1
    assert body["data"]["body_md"] == BODY_ONE
    assert body["data"]["acceptance"] == "It is built."


def test_set_refuses_bad_input(db, monkeypatch, tmp_path) -> None:
    code, _out, err = cli(monkeypatch, db, "spec", "set", ITEM, "--title", "T")
    assert code == 2 and "exactly one of --body or --body-file" in err
    code, _out, err = cli(
        monkeypatch, db, "spec", "set", "nope", "--title", "T", "--body", "B"
    )
    assert code == 1 and "item not found: nope" in err
    code, _out, err = cli(
        monkeypatch, db, "spec", "set", ITEM, "--title", "T", "--body", "B",
        env={"LEDGER_HARNESS": None},
    )
    assert code == 3 and "LEDGER_HARNESS" in err
    code, _out, err = cli(
        monkeypatch, db, "spec", "set", ITEM, "--title", "T", "--body", "B",
        "--merge-class", "two",
    )
    assert code == 2 and "invalid choice: 'two'" in err, err
    code, _out, err = cli(monkeypatch, db, "spec", "show", ITEM, "--title", "T")
    assert code == 2 and "does not take --title" in err
    assert spec_events(db) == []


def test_set_refuses_a_merge_class_outside_the_ladder(db, monkeypatch) -> None:
    code, _out, err = cli(
        monkeypatch, db, "spec", "set", ITEM, "--title", "T", "--body", "B",
        "--merge-class", "MC-9",
    )
    assert code == 2 and "invalid choice: 'MC-9'" in err, err
    assert spec_events(db) == [], "a refused merge class must append nothing"
    # The payload schema refuses it too, for a library caller of set_spec.
    with open_ledger(db) as lg:
        with pytest.raises(ValidationError, match="bad-type:merge_class"):
            set_spec(lg, ITEM, {"title": "T", "body_md": "B", "merge_class": "MC-9"},
                     harness="pytest")
    assert spec_events(db) == [], "the schema must refuse MC-9 before the append"
    for merge_class in ("MC-0", "MC-4"):
        code, _out, err = set_body(monkeypatch, db, f"body {merge_class}",
                                   "--merge-class", merge_class)
        assert code == 0, err
    assert [event.payload["merge_class"] for event in spec_events(db)] == ["MC-0", "MC-4"]


def _spec_rows(db: str) -> list:
    return [(event.id, event.payload) for event in spec_events(db)]


def test_generic_append_cannot_forge_a_spec(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    before = _spec_rows(db)
    code, _out, err = cli(
        monkeypatch, db, "append", "--kind", "item.spec", "--item", ITEM,
        "--payload", json.dumps(FORGED),
    )
    assert code == 3, f"generic append must refuse item.spec (rc={code}): {err}"
    assert "reserved-kind:item.spec" in err
    assert _spec_rows(db) == before, "generic append must not add a spec version"
    code, out, err = cli(monkeypatch, db, "spec", "show", ITEM)
    assert code == 0, err
    assert "forged body" not in out and "Then test it." not in out
    assert out.splitlines()[0] == f"spec v1\t{ITEM}\tevent={before[0][0]}"


def test_append_many_cannot_forge_a_spec(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    before = _spec_rows(db)
    with open_ledger(db, create=False) as lg:
        count = len(lg.events(item_id=ITEM))
    batch = [
        {"kind": "item.note", "payload": {"text": "a note in the same batch"}},
        {"kind": "item.spec", "payload": FORGED},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(batch)))
    code, _out, err = cli(monkeypatch, db, "append-many", ITEM)
    assert code == 3, f"append-many must refuse item.spec (rc={code}): {err}"
    assert "reserved-kind:item.spec" in err
    with open_ledger(db, create=False) as lg:
        assert len(lg.events(item_id=ITEM)) == count, "the whole batch must roll back"
    assert _spec_rows(db) == before, "append-many must not add a spec version"


def test_only_set_spec_holds_the_spec_writer_capability(db) -> None:
    with open_ledger(db) as lg:
        with pytest.raises(ValidationError, match="reserved-kind:item.spec"):
            lg.append(kind="item.spec", item_id=ITEM, payload=FORGED,
                      idempotency_key="forged-1", harness="pytest")
        with pytest.raises(ValidationError, match="reserved-kind:item.spec"):
            lg._append_loop_event(loop._LOOP_WRITER_CAPABILITY, kind="item.spec",
                                  item_id=ITEM, payload=FORGED,
                                  idempotency_key="forged-2", harness="pytest")
        with pytest.raises(ValidationError, match="spec-writer-capability-required"):
            lg._append_spec_event(object(), kind="item.spec", item_id=ITEM, payload=FORGED,
                                  idempotency_key="forged-3", harness="pytest")
        with pytest.raises(ValidationError, match="reserved-kind:loop.managed"):
            lg._append_spec_event(spec_verb._SPEC_WRITER_CAPABILITY, kind="loop.managed",
                                  item_id=ITEM, payload={},
                                  idempotency_key="forged-4", harness="pytest")
        assert lg.events(item_id=ITEM, kind="item.spec") == []
        assert lg.events(item_id=ITEM, kind="loop.managed") == []


def test_v_items_names_the_current_spec_version(db, monkeypatch) -> None:
    def version() -> int | None:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT spec_version FROM v_items WHERE item_id = ?", (ITEM,)
            ).fetchone()[0]
        finally:
            conn.close()

    assert version() is None
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    assert version() == 1
    assert set_body(monkeypatch, db, BODY_TWO)[0] == 0
    assert version() == 2


def test_claim_at_v1_then_spec_v2_prints_the_moved_line(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    code, body, err = cli_json(monkeypatch, db, "claim", ITEM)
    assert code == 0, err
    text = card_text(monkeypatch, db)
    assert "spec v1" in text.splitlines()
    assert "spec moved" not in text

    assert set_body(monkeypatch, db, BODY_TWO)[0] == 0
    lines = card_text(monkeypatch, db).splitlines()
    assert "spec v2" in lines
    assert "spec moved: v1 → v2" in lines, "the card must name the spec that moved past the claim"
    code, card, err = cli_json(monkeypatch, db, "card", ITEM)
    assert code == 0, err
    assert card["data"]["spec_moved"] == "spec moved: v1 → v2"
    assert card["data"]["spec_version"] == 2
    assert card["data"]["spec_claim_version"] == 1
    assert body["data"]["event"]["payload"]["spec_version"] == 1


def test_claim_refuses_when_the_spec_moves_mid_claim(db, monkeypatch) -> None:
    from ledger import lease

    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    real = lease.takeover_holder
    landed: list[bool] = []

    def spec_lands_meanwhile(*args, **kwargs):
        # The claim's first call runs after it read the item and before its
        # write transaction; the spec lands there, once. (guard_claim calls
        # this again inside the transaction, where a second writer would wait.)
        if not landed:
            landed.append(True)
            with open_ledger(db) as other:
                set_spec(other, ITEM, {"title": "Spec title", "body_md": BODY_TWO},
                         harness="pytest")
        return real(*args, **kwargs)

    monkeypatch.setattr(lease, "takeover_holder", spec_lands_meanwhile)
    code, body, _err = cli_json(monkeypatch, db, "claim", ITEM)
    assert code == 3, body
    assert body["error"]["code"] == "claim_changed"
    assert "the spec changed while this claim was being recorded" in body["error"]["message"]
    with open_ledger(db, create=False) as lg:
        assert lg.events(item_id=ITEM, kind="item.state") == []


def test_a_claim_on_an_item_without_a_spec_stamps_nothing(db, monkeypatch) -> None:
    code, body, err = cli_json(monkeypatch, db, "claim", ITEM)
    assert code == 0, err
    assert "spec_version" not in body["data"]["event"]["payload"]
    assert "spec -" in card_text(monkeypatch, db).splitlines()
    # A spec recorded after an unstamped claim moves nothing.
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    text = card_text(monkeypatch, db)
    assert "spec v1" in text.splitlines()
    assert "spec moved" not in text


def test_reclaim_at_the_current_version_clears_the_moved_line(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    assert cli(monkeypatch, db, "claim", ITEM)[0] == 0
    assert set_body(monkeypatch, db, BODY_TWO)[0] == 0
    assert "spec moved: v1 → v2" in card_text(monkeypatch, db)
    assert cli(monkeypatch, db, "claim", ITEM)[0] == 0
    assert "spec moved" not in card_text(monkeypatch, db)


def test_pick_prints_the_moved_line(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    assert cli(monkeypatch, db, "claim", ITEM)[0] == 0
    pick = ("pick", ITEM, "--question", "go?", "--option", "yes", "--option", "no",
            "--chosen", "yes", "--scope", "this item")
    code, out, err = cli(monkeypatch, db, *pick)
    assert code == 0, err
    assert "spec moved" not in out
    assert set_body(monkeypatch, db, BODY_TWO)[0] == 0
    code, out, err = cli(monkeypatch, db, *pick[:3], "again?", *pick[4:])
    assert code == 0, err
    assert out.splitlines()[-1] == "spec moved: v1 → v2"


PICK = ("pick", ITEM, "--question", "go?", "--option", "yes", "--option", "no",
        "--chosen", "yes", "--scope", "this item")


def pick_out(monkeypatch, db: str, n: int) -> str:
    code, out, err = cli(monkeypatch, db, *PICK[:3], f"go {n}?", *PICK[4:])
    assert code == 0, err
    return out


def moved_json(monkeypatch, db: str):
    code, card, err = cli_json(monkeypatch, db, "card", ITEM)
    assert code == 0, err
    return card["data"]["spec_moved"]


def test_source_only_move_does_not_print_the_moved_line(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE, "--source", "a.md")[0] == 0
    assert cli(monkeypatch, db, "claim", ITEM)[0] == 0
    assert set_body(monkeypatch, db, BODY_ONE, "--source", "b.md")[0] == 0
    assert [e.payload["version"] for e in spec_events(db)] == [1, 2]
    text = card_text(monkeypatch, db)
    assert "spec v2" in text.splitlines()
    assert "spec moved" not in text
    assert moved_json(monkeypatch, db) is None
    assert "spec moved" not in pick_out(monkeypatch, db, 1)


def test_content_change_prints_the_moved_line_with_a_source_version_between(
    db, monkeypatch
) -> None:
    assert set_body(monkeypatch, db, BODY_ONE, "--source", "a.md")[0] == 0
    assert cli(monkeypatch, db, "claim", ITEM)[0] == 0
    assert set_body(monkeypatch, db, BODY_ONE, "--source", "b.md")[0] == 0
    assert moved_json(monkeypatch, db) is None
    assert set_body(monkeypatch, db, BODY_TWO, "--source", "b.md")[0] == 0
    assert moved_json(monkeypatch, db) == "spec moved: v1 → v3"
    assert "spec moved: v1 → v3" in card_text(monkeypatch, db).splitlines()
    assert pick_out(monkeypatch, db, 1).splitlines()[-1] == "spec moved: v1 → v3"


def test_a_change_back_to_the_claimed_content_prints_no_moved_line(db, monkeypatch) -> None:
    # Equal content hash means the content the claim was built against is back.
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    assert cli(monkeypatch, db, "claim", ITEM)[0] == 0
    assert set_body(monkeypatch, db, BODY_TWO)[0] == 0
    assert moved_json(monkeypatch, db) == "spec moved: v1 → v2"
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    assert [e.payload["version"] for e in spec_events(db)] == [1, 2, 3]
    assert moved_json(monkeypatch, db) is None
    assert "spec moved" not in card_text(monkeypatch, db)


def test_a_missing_claimed_spec_event_falls_back_to_the_version_numbers() -> None:
    from ledger.card import spec_content_hash, spec_moved_line

    assert spec_content_hash([], 1) is None
    assert spec_moved_line(1, 2, None, "h") == "spec moved: v1 → v2"
    assert spec_moved_line(1, 2, "h", None) == "spec moved: v1 → v2"
    assert spec_moved_line(1, 2, "h", "h") is None
    assert spec_moved_line(1, 2, "h", "g") == "spec moved: v1 → v2"


def test_loop_take_stamps_the_spec_version_and_a_hand_off_keeps_it(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    with open_ledger(db) as lg:
        take = loop.take_item(lg, ITEM, "work", idempotency_key="m1", claim_key="c1")
        assert take.payload["spec_version"] == 1
        set_spec(lg, ITEM, {"title": "Spec title", "body_md": BODY_TWO}, harness="pytest")
        assert item_spec_moved(lg, ITEM) == "spec moved: v1 → v2"
        # A session step's hand-off rewrites the claim unstamped: same claim.
        loop.claim_operation(lg, ITEM, "build", "op-1", holder="work", hand_off=True)
        latest = lg.events(item_id=ITEM, kind="item.state")[-1]
        assert "spec_version" not in latest.payload and "expires_at" not in latest.payload
        assert item_spec_moved(lg, ITEM) == "spec moved: v1 → v2"
        card = project_card(lg, ITEM)
        assert (card.spec_claim_version, card.spec_version) == (1, 2)


def test_another_holders_claim_ends_the_stamped_run(db, monkeypatch) -> None:
    assert set_body(monkeypatch, db, BODY_ONE)[0] == 0
    with open_ledger(db) as lg:
        lg.append(kind="item.state", item_id=ITEM, idempotency_key="s1", harness="pytest",
                  payload={"status": "claimed", "claim": "alice", "spec_version": 1})
        lg.append(kind="item.state", item_id=ITEM, idempotency_key="s2", harness="pytest",
                  payload={"status": "claimed", "claim": "bob"})
        set_spec(lg, ITEM, {"title": "Spec title", "body_md": BODY_TWO}, harness="pytest")
        item = lg.item(ITEM)
        assert claim_spec_version(lg.events(item_id=ITEM), item) is None
        assert item_spec_moved(lg, ITEM) is None


def test_spec_versions_are_computed_in_the_write_transaction(db) -> None:
    with open_ledger(db) as lg:
        first, created = set_spec(lg, ITEM, {"title": "T", "body_md": "one"}, harness="pytest")
        assert created and first.payload["version"] == 1
        again, created = set_spec(lg, ITEM, {"title": "T", "body_md": "one"}, harness="pytest")
        assert not created and again.id == first.id
        second, created = set_spec(lg, ITEM, {"title": "T", "body_md": "two"}, harness="pytest")
        assert created and second.payload["version"] == 2
        assert second.idempotency_key == f"spec:{ITEM}:v2"
        lg.verify()


def test_a_ledger_at_0007_migrates_and_keeps_every_view_column(tmp_path) -> None:
    path = tmp_path / "old.db"
    migrations = store.load_migrations()
    conn = store.connect(path)
    try:
        store.migrate(conn, [m for m in migrations if m.version <= 7])
        before = {
            view: [row[1] for row in conn.execute(f"PRAGMA table_info({view})")]
            for view in ("v_items", "v_ready", "v_blocked", "v_leases", "v_open_decisions")
        }
    finally:
        conn.close()
    with open_ledger(path) as lg:
        conn = lg._conn
        after = {
            view: [row[1] for row in conn.execute(f"PRAGMA table_info({view})")]
            for view in before
        }
    for view, columns in before.items():
        assert after[view][: len(columns)] == columns, view
    assert after["v_items"] == before["v_items"] + [
        "spec_version", "parent_id", "children_open",
    ]
    assert after["v_leases"] == before["v_leases"]
    assert after["v_open_decisions"] == before["v_open_decisions"]
