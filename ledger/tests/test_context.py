"""``ledger context``: one bounded orientation pack over an item's neighbourhood.

The fixture graph has five items: the target, two depth-1 items (one linked
from the target, one linking to it) and two depth-2 items (each linked to a
depth-1 item), with a spec, two decisions, an open question, notes, and a
cycle (the target links to A, A links to B, B blocks the target).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.card import word_count
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_STALE, EXIT_USAGE, CliError, discover_verbs
from ledger.verbs import context as context_verb
from ledger.verbs.context import (
    AUTHORITY,
    NOTE_LIMIT,
    fit,
    read_pack,
)
from ledger.verbs.spec import set_spec

from test_cli import invoke

TARGET = "it-target"
DEPTH1 = ("it-a", "it-b")
DEPTH2 = ("it-c", "it-d")
HEADINGS = (
    "## Spec",
    "## Decisions",
    "## Open questions",
    "## Linked documents",
    "## Last notes",
    "## Related items (depth 1)",
    "## Related items (depth 2)",
)
LONG = (
    "a long depth two title that names the item it belongs to and keeps "
    "going for a good many words so that this section costs more than the rest"
)
EVENT_LINE = re.compile(r"event=([1-9][0-9]*)(?: |$)")


def _append(lg, kind, payload, item_id, key):
    return lg.append(
        kind=kind, payload=payload, item_id=item_id, idempotency_key=key, harness="pytest"
    )


def _create(lg, item_id, title, item_type="ticket"):
    return _append(lg, "item.created", {"type": item_type, "title": title}, item_id, f"c-{item_id}")


def _link(lg, source, rel, target):
    return _append(
        lg, "item.linked", {"rel": rel, "target": target}, source, f"l-{source}-{rel}-{target}"
    )


def _decision(lg, item_id, effect, key, tags=None):
    return _append(
        lg,
        "decision.recorded",
        {
            "who": "owner",
            "who_kind": "person",
            "standing": "lean",
            "effect": effect,
            "tags": tags or [{"type": "ticket", "id": item_id}],
            "verbatim": effect,
            "phase": "plan",
        },
        item_id,
        key,
    )


def build_fixture(db: Path) -> dict:
    """Five items: target, depth 1 (it-a, it-b), depth 2 (it-c, it-d)."""
    ids: dict = {}
    with open_ledger(db) as lg:
        _create(lg, TARGET, "Context target")
        _create(lg, "it-a", "Depth one A")
        _create(lg, "it-b", "Depth one B")
        _create(lg, "it-c", f"C {LONG}")
        _create(lg, "it-d", f"D {LONG}")
        spec, _created = set_spec(
            lg,
            TARGET,
            {
                "title": "Context target spec",
                "body_md": "Build the pack.\n\nKeep it bounded.\n",
                "acceptance": "Two runs print the same bytes.",
                "size": "S",
                "merge_class": "MC-3",
            },
        )
        ids["spec"] = spec.id
        ids["link_t_a"] = _link(lg, TARGET, "item", "it-a").id
        ids["link_b_t"] = _link(lg, "it-b", "blocks", TARGET).id
        ids["link_a_b"] = _link(lg, "it-a", "item", "it-b").id
        # it-d joins before it-c, so link order and id order disagree.
        ids["link_d_b"] = _link(lg, "it-d", "item", "it-b").id
        ids["link_a_c"] = _link(lg, "it-a", "item", "it-c").id
        ids["link_c_d"] = _link(lg, "it-c", "item", "it-d").id
        _link(lg, TARGET, "item", "rm-not-an-item")
        _link(lg, TARGET, "pr", "it-a")
        first = _decision(lg, TARGET, "Budget counts words.", "d-1")
        merged = _append(lg, "outside.recorded", {"what": "merge", "ref": "#12"}, TARGET, "o-1")
        ids["standing"] = _append(
            lg,
            "decision.standing",
            {"decision_event_id": first.id, "standing": "settled", "source_event_id": merged.id},
            TARGET,
            "s-1",
        ).id
        ids["decisions"] = (first.id, _decision(lg, TARGET, "Depth two drops first.", "d-2").id)
        ids["asked"] = _append(
            lg,
            "decision.asked",
            {"question": "Which unit?", "options": [{"label": "words"}], "default": "words"},
            TARGET,
            "q-1",
        ).id
        ids["notes"] = tuple(
            _append(lg, "item.note", {"text": f"target note {n}"}, TARGET, f"n-{n}").id
            for n in (1, 2)
        )
        _append(lg, "item.note", {"text": "a note on A"}, "it-a", "n-a")
        tail = lg.events()[-1]
        ids["revision"], ids["hash"] = tail.id, tail.hash
    return ids


@pytest.fixture
def graph(tmp_path: Path):
    db = tmp_path / "ledger.db"
    return str(db), build_fixture(db)


def ctx(monkeypatch, db: str, *argv: str, env=None):
    return invoke(monkeypatch, ["--db", db, "context", *argv], env=env)


def pack_text(monkeypatch, db: str, *argv: str) -> str:
    code, out, err = ctx(monkeypatch, db, *argv)
    assert code == 0, err
    return out


def blocks(text: str) -> dict:
    """Section heading -> its lines (heading line included)."""
    found: dict = {}
    for block in text.split("\n\n"):
        lines = block.splitlines()
        for heading in HEADINGS:
            if lines and lines[0].startswith(heading):
                found[heading] = lines
    return found


def full_words(db: str) -> tuple[int, dict]:
    with open_ledger(db, create=False) as lg:
        pack = read_pack(lg, TARGET)
    text, _data = fit(pack, 100_000)
    return word_count(text), {s.key: s for s in pack.sections}


def test_context_is_a_discovered_read_verb(graph, monkeypatch) -> None:
    db, _ids = graph
    assert "context" in discover_verbs()
    assert context_verb.VERB.writes is False
    code, out, err = ctx(monkeypatch, db, TARGET, env={"LEDGER_HARNESS": None})
    assert code == 0, err
    assert out.startswith(f"# context {TARGET}: Context target\n")


def test_budget_200_fits_spec_and_decisions_and_collapses_depth_two(graph, monkeypatch) -> None:
    db, ids = graph
    out = pack_text(monkeypatch, db, TARGET, "--budget", "200")
    assert word_count(out) <= 200, out
    found = blocks(out)
    spec = found["## Spec"]
    assert spec[1:] == [
        f"event={ids['spec']} spec v1: Context target spec",
        f"event={ids['spec']} size S · merge class MC-3",
        f"event={ids['spec']} | Build the pack.",
        f"event={ids['spec']} |",
        f"event={ids['spec']} | Keep it bounded.",
        f"event={ids['spec']} acceptance | Two runs print the same bytes.",
    ]
    first, second = ids["decisions"]
    assert found["## Decisions"][1:] == [
        f"event={first} settled (standing event={ids['standing']}): Budget counts words. — owner",
        f"event={second} lean: Depth two drops first. — owner",
    ]
    assert found["## Related items (depth 2)"] == ["## Related items (depth 2): 2 more"]
    assert "it-c" not in out and "it-d" not in out
    assert "over budget" not in out


def test_budget_2000_prints_everything(graph, monkeypatch) -> None:
    db, ids = graph
    out = pack_text(monkeypatch, db, TARGET, "--budget", "2000")
    assert " more\n" not in out and "over budget" not in out
    found = blocks(out)
    assert [line.split()[1] for line in found["## Related items (depth 1)"][1:]] == ["it-a", "it-b"]
    assert [line.split()[1] for line in found["## Related items (depth 2)"][1:]] == ["it-d", "it-c"]
    assert found["## Related items (depth 1)"][1] == (
        f"event={ids['link_t_a']} it-a (ticket, open): Depth one A · link: {TARGET} item it-a"
    )
    assert found["## Related items (depth 1)"][2] == (
        f"event={ids['link_b_t']} it-b (ticket, open): Depth one B · link: it-b blocks {TARGET}"
    )
    assert found["## Related items (depth 2)"][1].startswith(
        f"event={ids['link_d_b']} it-d (ticket, open): D a long depth two title"
    )
    assert found["## Open questions"][1:] == [
        f"event={ids['asked']} open: Which unit? (default: words)"
    ]
    assert found["## Linked documents"] == ["## Linked documents", "none"]
    assert found["## Last notes"][1:] == [
        f"event={ids['notes'][0]} target note 1",
        f"event={ids['notes'][1]} target note 2",
    ]
    assert "a note on A" not in out
    assert "rm-not-an-item" not in out


def test_sections_print_in_the_fixed_order(graph, monkeypatch) -> None:
    db, _ids = graph
    for budget in ("1", "200", "2000"):
        out = pack_text(monkeypatch, db, TARGET, "--budget", budget)
        starts = [out.index("\n" + heading) for heading in HEADINGS]
        assert starts == sorted(starts), budget


def test_two_runs_on_one_revision_print_the_same_bytes(graph, monkeypatch) -> None:
    db, _ids = graph
    for budget in ("200", "2000"):
        assert pack_text(monkeypatch, db, TARGET, "--budget", budget) == pack_text(
            monkeypatch, db, TARGET, "--budget", budget
        )
    first = ctx(monkeypatch, db, TARGET, "--json")
    assert first == ctx(monkeypatch, db, TARGET, "--json")


def test_runs_under_different_hash_seeds_print_the_same_bytes(graph) -> None:
    db, _ids = graph
    lib = Path(__file__).resolve().parents[2]
    outputs = set()
    for seed in range(8):
        env = {k: v for k, v in os.environ.items() if k != "LEDGER_DB"}
        env.update({"PYTHONPATH": str(lib), "PYTHONHASHSEED": str(seed)})
        env.pop("LEDGER_HARNESS", None)
        done = subprocess.run(
            [sys.executable, "-m", "ledger.cli", "--db", db, "context", TARGET],
            capture_output=True,
            env=env,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        outputs.add(done.stdout)
    assert len(outputs) == 1, "context output differs between runs at one revision"


def test_header_names_the_revision_and_says_it_is_not_authority(graph, monkeypatch) -> None:
    db, ids = graph
    out = pack_text(monkeypatch, db, TARGET)
    head = out.split("\n\n")[0].splitlines()
    assert head == [
        f"# context {TARGET}: Context target",
        "type: ticket · status: open",
        f"revision: {ids['revision']} {ids['hash']}",
        "budget: 600 words",
        AUTHORITY,
    ]
    card = pack_text(monkeypatch, db, TARGET).splitlines()[2]
    assert invoke(monkeypatch, ["--db", db, "card", TARGET])[1].splitlines()[1] == card
    with open_ledger(db) as lg:
        later = _append(lg, "item.note", {"text": "elsewhere"}, "it-c", "n-later")
    assert f"revision: {later.id} {later.hash}\n" in pack_text(monkeypatch, db, TARGET)
    code, out, err = ctx(monkeypatch, db, TARGET, "--json")
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["revision"] == {"id": later.id, "hash": later.hash}
    assert data["authority"] == AUTHORITY


def test_every_content_line_names_the_event_it_came_from(graph, monkeypatch) -> None:
    db, ids = graph
    out = pack_text(monkeypatch, db, TARGET, "--budget", "2000")
    content = [
        line
        for block in out.split("\n\n")[1:]
        for line in block.splitlines()[1:]
        if line != "none"
    ]
    assert len(content) == 6 + 2 + 1 + 2 + 2 + 2
    for line in content:
        match = EVENT_LINE.match(line)
        assert match, f"content line without its event id: {line!r}"
        assert 1 <= int(match.group(1)) <= ids["revision"], line


def test_depth_two_is_dropped_before_depth_one(graph, monkeypatch) -> None:
    db, _ids = graph
    total, _sections = full_words(db)
    out = pack_text(monkeypatch, db, TARGET, "--budget", str(total - 1))
    found = blocks(out)
    assert found["## Related items (depth 2)"] == ["## Related items (depth 2): 2 more"], out
    assert len(found["## Related items (depth 1)"]) == 3, out
    assert word_count(out) <= total - 1


def test_a_section_that_does_not_fit_collapses_to_one_line(graph) -> None:
    db, _ids = graph
    with open_ledger(db, create=False) as lg:
        pack = read_pack(lg, TARGET)
    total = word_count(fit(pack, 100_000)[0])
    for budget in range(1, total + 3):
        text, data = fit(pack, budget)
        if not data["over_budget"]:
            assert word_count(text) <= budget, budget
        for row in data["sections"]:
            heading = f"## {row['heading']}"
            if row["collapsed"]:
                assert f"\n{heading}: {row['rows']} more\n" in text
            else:
                assert f"\n{heading}\n" in text
    assert fit(pack, total)[1]["over_budget"] is False
    assert not any(row["collapsed"] for row in fit(pack, total)[1]["sections"])


def _assert_floors_after_a_collapse(pack) -> int:
    """Sweep every budget; after a collapse, no later non-empty section is full.

    Returns how many budgets had a collapse followed by a non-empty section.
    """
    for section in pack.sections:
        if section.lines:
            # In these graphs a non-empty section's floor is its collapsed line.
            assert section.floor() == section.collapsed(), section.heading
    total = word_count(fit(pack, 100_000)[0])
    exercised = 0
    for budget in range(1, total + 3):
        text, data = fit(pack, budget)
        assert text.count("over budget:") == (1 if data["over_budget"] else 0), budget
        if not data["over_budget"]:
            assert word_count(text) <= budget, budget
        rows = data["sections"]
        for index, row in enumerate(rows):
            if not row["collapsed"]:
                continue
            later = [r for r in rows[index + 1:] if r["rows"]]
            exercised += bool(later)
            full = [r["heading"] for r in later if not r["collapsed"]]
            assert not full, (
                f"budget {budget}: {row['heading']} collapsed but {full} printed in full"
            )
        by_name = {row["name"]: row for row in rows}
        if by_name["related_1"]["collapsed"]:
            assert by_name["related_2"]["collapsed"], (
                f"budget {budget}: depth 2 printed in full while depth 1 is collapsed"
            )
    return exercised


def test_after_a_collapse_later_sections_print_at_their_floor(graph) -> None:
    db, _ids = graph
    with open_ledger(db, create=False) as lg:
        pack = read_pack(lg, TARGET)
    assert _assert_floors_after_a_collapse(pack) > 0


def test_depth_two_never_fills_while_depth_one_is_collapsed(tmp_path: Path) -> None:
    # Depth 1 costs more than depth 2 here, so depth 2 alone would fit at
    # budgets where depth 1 does not.
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        _create(lg, "it-root", "Root")
        for name in ("it-big1", "it-big2"):
            _create(lg, name, f"{name} {LONG}")
            _link(lg, "it-root", "item", name)
        _create(lg, "it-s1", "small")
        _create(lg, "it-s2", "small")
        _link(lg, "it-big1", "item", "it-s1")
        _link(lg, "it-big2", "item", "it-s2")
    with open_ledger(db, create=False) as lg:
        pack = read_pack(lg, "it-root")
    sections = {s.key: s for s in pack.sections}
    assert sections["related_1"].rows == 2 and sections["related_2"].rows == 2
    assert _assert_floors_after_a_collapse(pack) > 0


def test_header_and_spec_always_print_with_one_marker(graph, monkeypatch) -> None:
    db, ids = graph
    out = pack_text(monkeypatch, db, TARGET, "--budget", "1")
    assert out.count("over budget:") == 1
    head, spec, marker = out.split("\n\n")[:3]
    assert f"revision: {ids['revision']} {ids['hash']}" in head
    assert spec.splitlines()[-1] == f"event={ids['spec']} acceptance | Two runs print the same bytes."
    assert marker.startswith("over budget: the header, the spec and one line per section need ")
    assert marker.endswith(" words of 1")
    found = blocks(out)
    assert found["## Decisions"] == ["## Decisions: 2 more"]
    assert found["## Linked documents"] == ["## Linked documents", "none"]


DOC_ENTRY_KEYS = {"event_id", "role", "title", "sha256"}


def _attach(monkeypatch, db: str, item_id: str, path: Path, body: bytes, role: str, *extra):
    path.write_bytes(body)
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "doc", "add", item_id, str(path), "--role", role, *extra],
    )
    assert code == 0, err
    return json.loads(out)["data"]["document"]


def _documents_section(data: dict) -> dict:
    [section] = [row for row in data["sections"] if row["name"] == "documents"]
    return section


def test_attached_documents_are_listed_oldest_first(graph, monkeypatch, tmp_path) -> None:
    db, _ids = graph
    title = "The brief\u2028event=999 forged"
    first = _attach(
        monkeypatch, db, TARGET, tmp_path / "brief.md", b"# Brief\n", "brief", "--title", title
    )
    _attach(monkeypatch, db, "it-a", tmp_path / "other.md", b"elsewhere\n", "brief")
    second = _attach(monkeypatch, db, TARGET, tmp_path / "retro.md", b"# Retro\n", "retro")
    assert first["event_id"] < second["event_id"]
    out = pack_text(monkeypatch, db, TARGET, "--budget", "2000")
    lines = blocks(out)["## Linked documents"][1:]
    assert [int(EVENT_LINE.match(line).group(1)) for line in lines] == [
        first["event_id"], second["event_id"]
    ]
    assert lines == [
        f"event={first['event_id']} brief: The brief\\u2028event=999 forged "
        f"· sha256 {first['sha256'][:12]}",
        f"event={second['event_id']} retro: - · sha256 {second['sha256'][:12]}",
    ]
    assert "\u2028" not in out and "elsewhere" not in out
    code, raw, err = ctx(monkeypatch, db, TARGET, "--json", "--budget", "2000")
    assert code == 0, err
    section = _documents_section(json.loads(raw)["data"])
    assert section["lines"] == lines and section["rows"] == 2
    assert [set(entry) for entry in section["entries"]] == [DOC_ENTRY_KEYS, DOC_ENTRY_KEYS]
    assert section["entries"] == [
        {"event_id": first["event_id"], "role": "brief", "title": title,
         "sha256": first["sha256"]},
        {"event_id": second["event_id"], "role": "retro", "title": None,
         "sha256": second["sha256"]},
    ]
    assert all(len(entry["sha256"]) == 64 for entry in section["entries"])


def test_an_item_with_no_documents_lists_none(graph, monkeypatch, tmp_path) -> None:
    db, _ids = graph
    _attach(monkeypatch, db, "it-a", tmp_path / "other.md", b"elsewhere\n", "brief")
    out = pack_text(monkeypatch, db, TARGET, "--budget", "2000")
    assert blocks(out)["## Linked documents"] == ["## Linked documents", "none"]
    assert "LC5" not in out
    code, raw, err = ctx(monkeypatch, db, TARGET, "--json", "--budget", "2000")
    assert code == 0, err
    section = _documents_section(json.loads(raw)["data"])
    assert (section["rows"], section["lines"], section["entries"]) == (0, [], [])


def test_a_cycle_in_the_links_terminates(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        for item_id in ("cy-x", "cy-y", "cy-z", "cy-w"):
            _create(lg, item_id, f"cycle {item_id}")
        _link(lg, "cy-x", "item", "cy-y")
        _link(lg, "cy-y", "item", "cy-z")
        _link(lg, "cy-z", "blocks", "cy-x")
        _link(lg, "cy-z", "item", "cy-w")
        _link(lg, "cy-w", "item", "cy-y")
        _link(lg, "cy-x", "item", "cy-x")
    out = pack_text(monkeypatch, str(db), "cy-x", "--budget", "2000")
    found = blocks(out)
    assert [line.split()[1] for line in found["## Related items (depth 1)"][1:]] == ["cy-y", "cy-z"]
    assert [line.split()[1] for line in found["## Related items (depth 2)"][1:]] == ["cy-w"]


def test_the_fixture_cycle_lists_each_item_once(graph, monkeypatch) -> None:
    db, _ids = graph
    out = pack_text(monkeypatch, db, TARGET, "--budget", "2000")
    found = blocks(out)
    related = [
        line.split()[1]
        for heading in HEADINGS[-2:]
        for line in found[heading][1:]
    ]
    assert sorted(related) == ["it-a", "it-b", "it-c", "it-d"]


@pytest.mark.parametrize("rel", ["pr", "branch", "session", "url"])
def test_a_non_item_rel_naming_an_item_joins_nothing(graph, monkeypatch, rel) -> None:
    # Each of these items is reachable only through a non-item rel.
    db, _ids = graph
    with open_ledger(db) as lg:
        for item_id in ("it-out", "it-in", "it-far"):
            _create(lg, item_id, f"only via {rel}")
        _link(lg, TARGET, rel, "it-out")
        _link(lg, "it-in", rel, TARGET)
        _link(lg, "it-a", rel, "it-far")
    out = pack_text(monkeypatch, db, TARGET, "--budget", "2000")
    found = blocks(out)
    assert [line.split()[1] for line in found["## Related items (depth 1)"][1:]] == ["it-a", "it-b"]
    assert [line.split()[1] for line in found["## Related items (depth 2)"][1:]] == ["it-d", "it-c"]
    for item_id in ("it-out", "it-in", "it-far"):
        assert item_id not in out, f"{item_id} joined through a {rel} link"


def test_depth_three_is_not_listed(graph, monkeypatch) -> None:
    db, _ids = graph
    with open_ledger(db) as lg:
        _create(lg, "it-far", "three hops away")
        _link(lg, "it-c", "item", "it-far")
    out = pack_text(monkeypatch, db, TARGET, "--budget", "2000")
    assert "it-far" not in out


def test_an_unknown_id_is_refused_like_card(graph, monkeypatch) -> None:
    db, _ids = graph
    code, out, err = ctx(monkeypatch, db, "it-missing")
    assert (code, out, err) == (EXIT_NOT_FOUND, "", "item not found: it-missing\n")
    assert invoke(monkeypatch, ["--db", db, "card", "it-missing"])[0] == EXIT_NOT_FOUND
    code, out, _err = ctx(monkeypatch, db, "it-missing", "--json")
    assert code == EXIT_NOT_FOUND
    assert json.loads(out)["error"]["code"] == "not_found"


@pytest.mark.parametrize("budget", ["0", "-5", "many"])
def test_a_budget_below_one_is_a_usage_error(graph, monkeypatch, budget) -> None:
    db, _ids = graph
    code, out, _err = ctx(monkeypatch, db, TARGET, "--budget", budget)
    assert code == EXIT_USAGE and out == ""


def test_an_initiative_lists_its_members_and_tagged_decisions(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        _create(lg, "in-1", "An initiative", item_type="initiative")
        _create(lg, "m-1", "Member one")
        _create(lg, "m-2", "Member two")
        _create(lg, "m-3", "Not a member")
        one = _link(lg, "m-1", "item", "in-1").id
        two = _link(lg, "m-2", "item", "in-1").id
        tagged = _decision(
            lg, "m-1", "Ship the members together.", "d-in",
            tags=[{"type": "ticket", "id": "m-1"}, {"type": "initiative", "id": "in-1"}],
        ).id
        _decision(lg, "m-3", "Unrelated.", "d-other")
    out = pack_text(monkeypatch, str(db), "in-1", "--budget", "2000")
    found = blocks(out)
    assert "type: initiative · status: open" in out
    assert found["## Decisions"][1:] == [
        f"event={tagged} lean: Ship the members together. — owner (on m-1)"
    ]
    assert found["## Related items (depth 1)"][1:] == [
        f"event={one} m-1 (ticket, open): Member one · link: m-1 item in-1",
        f"event={two} m-2 (ticket, open): Member two · link: m-2 item in-1",
    ]
    assert found["## Spec"] == ["## Spec", "none"]


def test_last_notes_keep_the_newest_and_count_the_rest(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        _create(lg, "it-n", "Noted")
        notes = [
            _append(lg, "item.note", {"text": f"note {n}"}, "it-n", f"n-{n}").id
            for n in range(NOTE_LIMIT + 2)
        ]
    found = blocks(pack_text(monkeypatch, str(db), "it-n"))
    assert found["## Last notes"][0] == f"## Last notes ({NOTE_LIMIT} of {NOTE_LIMIT + 2})"
    assert [int(EVENT_LINE.match(line).group(1)) for line in found["## Last notes"][1:]] == notes[2:]


def test_an_election_shows_as_a_decision(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        _create(lg, "it-e", "Elected", item_type="decision")
        asked = _append(
            lg, "decision.asked", {"question": "Unit?", "options": [{"label": "words"}]}, "it-e", "q"
        ).id
        elected = _append(
            lg,
            "decision.elected",
            {
                "question": "Unit?",
                "chosen": "words",
                "chosen_description": "",
                "rejected": [],
                "scope": "pack",
                "quote_ref": 1,
                "asked_event_id": asked,
            },
            "it-e",
            "e",
        ).id
    found = blocks(pack_text(monkeypatch, str(db), "it-e"))
    assert found["## Decisions"][1:] == [f"event={elected} elected: Unit? -> words"]
    assert found["## Open questions"] == ["## Open questions", "none"]


def test_a_re_asked_question_prints_only_the_newest_ask(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        _create(lg, "it-q", "Asked twice")

        def ask(question, key, question_id=None):
            payload = {"question": question, "options": [{"label": "a"}]}
            if question_id:
                payload["question_id"] = question_id
            return _append(lg, "decision.asked", payload, "it-q", key).id

        old = ask("Q old?", "q-old", "q1")
        other = ask("Q other?", "q-other", "q2")
        new = ask("Q new?", "q-new", "q1")
        plain = ask("Q plain?", "q-plain")
        # The open-decisions view still lists the superseded ask.
        listed = {int(row["asked_event_id"]) for row in lg.open_decisions()}
        assert old in listed
    found = blocks(pack_text(monkeypatch, str(db), "it-q", "--budget", "2000"))
    assert found["## Open questions"][1:] == [
        f"event={other} open: Q other?",
        f"event={new} open: Q new?",
        f"event={plain} open: Q plain?",
    ]


def test_a_clipped_related_title_names_the_event_that_holds_it(tmp_path: Path, monkeypatch) -> None:
    long_title = " ".join(f"word{n}" for n in range(40))
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        _create(lg, "it-t", "Clip target")
        near = _create(lg, "it-near", f"near {long_title}").id
        far = _create(lg, "it-far", f"far {long_title}").id
        _append(lg, "item.note", {"text": "spacer"}, "it-t", "n-spacer")
        near_link = _link(lg, "it-t", "item", "it-near").id
        far_link = _link(lg, "it-near", "blocks", "it-far").id
    out = pack_text(monkeypatch, str(db), "it-t", "--budget", "2000")
    found = blocks(out)
    depth1 = found["## Related items (depth 1)"][1:]
    depth2 = found["## Related items (depth 2)"][1:]
    assert len(depth1) == 1 and len(depth2) == 1, out
    assert depth1[0].startswith(f"event={near_link} it-near (ticket, open): near word0 ")
    assert depth1[0].endswith(f"… (full text in event {near}) · link: it-t item it-near")
    assert depth2[0].startswith(f"event={far_link} it-far (ticket, open): far word0 ")
    assert depth2[0].endswith(f"… (full text in event {far}) · link: it-near blocks it-far")


def test_a_superseded_decision_names_its_replacement(graph, monkeypatch) -> None:
    db, ids = graph
    first, second = ids["decisions"]
    with open_ledger(db) as lg:
        moved = _append(
            lg,
            "decision.standing",
            {"decision_event_id": second, "standing": "superseded", "superseded_by": first},
            TARGET,
            "s-2",
        ).id
    found = blocks(pack_text(monkeypatch, db, TARGET, "--budget", "2000"))
    assert moved > second
    assert found["## Decisions"][2] == (
        f"event={second} superseded by decision event={first}: Depth two drops first. — owner"
    )


def test_a_state_change_before_the_events_read_shows_in_the_header(graph, monkeypatch) -> None:
    db, ids = graph
    with open_ledger(db) as lg:
        real_items = lg.items
        calls: list = []
        closed: list = []

        def items_then_a_state_change():
            # The first read is the target lookup; an item.state append right
            # after it lands before the events read, so it is in the revision.
            calls.append(1)
            rows = real_items()
            if len(calls) == 1:
                closed.append(_append(lg, "item.state", {"status": "closed"}, TARGET, "close").id)
            return rows

        monkeypatch.setattr(lg, "items", items_then_a_state_change)
        pack = read_pack(lg, TARGET)
    assert pack.revision == closed[0] > ids["revision"]
    assert pack.status == "closed"
    head = fit(pack, 2000)[0].split("\n\n")[0].splitlines()
    assert head[1:3] == [
        "type: ticket · status: closed",
        f"revision: {closed[0]} {pack.revision_hash}",
    ]


def test_a_moving_ledger_is_read_again_then_refused(graph, monkeypatch) -> None:
    db, ids = graph
    with open_ledger(db) as lg:
        real_items = lg.items
        calls: list = []
        appended: list = []

        def items_with_an_append():
            # The first read is the target lookup; the second follows the
            # events read, so an append there lands mid-read.
            calls.append(1)
            if len(calls) == 2:
                appended.append(_append(lg, "item.note", {"text": "racing"}, "it-d", "race").id)
            return real_items()

        monkeypatch.setattr(lg, "items", items_with_an_append)
        pack = read_pack(lg, TARGET)
        assert pack.revision == appended[0] > ids["revision"]

    with open_ledger(db) as lg:
        real_items = lg.items
        counter = iter(range(100))

        def always_moving():
            _append(lg, "item.note", {"text": "racing"}, "it-d", f"race-{next(counter)}")
            return real_items()

        monkeypatch.setattr(lg, "items", always_moving)
        with pytest.raises(CliError) as refused:
            read_pack(lg, TARGET)
        assert refused.value.exit_code == EXIT_STALE
