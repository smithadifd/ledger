"""``ledger search --mentions ID`` and the mentioned-in line on an item hit.

A ruling about an item often sits on another item (its parent, a readout, an
initiative) and names the item's id in its text. ``--mentions ID`` lists the
documents on other items whose text names ID as a whole token, decision
documents first; a normal text search prints up to two such documents under an
item hit. Every ledger here is synthetic and built under tmp_path.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger.cli import main
from ledger.search import MAX_LIMIT, IndexNotCurrent, Mention, UnknownItem
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE
from ledger.verbs import search as search_verb
from ledger.verbs.spec import set_spec

PARENT = "parent-item"
CHILD = "child-item"
SIBLING = "child-item-2"
READOUT = "readout-item"
BYSTANDER = "bystander-item"
RULING = (
    "decided: the map is attested by machine, never by a reader; the work "
    f"belongs to {CHILD} and nothing else"
)
NORMAL_WORD = "tooltips"


def invoke(monkeypatch, argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_json(monkeypatch, db: Path, *argv: str) -> tuple[int, dict]:
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(db), *argv])
    assert "Traceback" not in err
    return code, json.loads(out)


def _append(lg, kind: str, item_id: str | None, payload: dict, key: str):
    return lg.append(kind=kind, item_id=item_id, payload=payload,
                     idempotency_key=key, harness="fixture")


def _doc(item_id: str, event) -> str:
    """The search document id of an event on an item."""
    return f"{item_id}:{event.id}"


def _error(exc_type, call, *args, **kwargs):
    """The ``exc_type`` the call raises; an assertion failure when it raises none."""
    try:
        call(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"{call.__name__} raised no {exc_type.__name__}")


def _config(db: Path, roots: list[Path] | None = None) -> None:
    (db.parent / "config.json").write_text(
        json.dumps({"search_roots": [str(root) for root in roots or []]}), encoding="utf-8")


def _ids(monkeypatch, db: Path, *argv: str) -> list[str]:
    code, body = run_json(monkeypatch, db, "search", *argv)
    assert code == EXIT_OK, body
    return [card["id"] for card in body["data"]["cards"]]


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch) -> dict:
    """A parent whose ruling names the child, a readout whose spec, stop and
    recorded decision name it too, and notes that only look like naming it."""
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    db = tmp_path / "state" / "home.db"
    docs: dict[str, str] = {}
    with open_ledger(db) as lg:
        _append(lg, "item.created", PARENT,
                {"type": "initiative", "title": "Readout of the first round"}, "parent")
        _append(lg, "item.created", CHILD,
                {"type": "ticket", "title": "Map tooltips"}, "child")
        _append(lg, "item.created", SIBLING,
                {"type": "ticket", "title": "Second tooltips"}, "sibling")
        _append(lg, "item.created", READOUT,
                {"type": "initiative", "title": "Round summary"}, "readout")
        _append(lg, "item.created", BYSTANDER,
                {"type": "ticket", "title": "Unrelated chores"}, "bystander")
        # A plain note: only part of the parent's item document.
        _append(lg, "item.note", PARENT,
                {"text": f"round 3 receipt recorded; lane for {CHILD} closed"}, "plain")
        docs["ruling"] = _doc(PARENT, _append(
            lg, "item.note", PARENT, {"text": RULING}, "ruling"))
        # The rest of the documents that name the child; each ranks above the
        # ruling on bm25 alone (a short text that repeats the id).
        spec, _created = set_spec(
            lg, READOUT,
            {"title": f"{CHILD} {CHILD}", "body_md": f"{CHILD} {CHILD} {CHILD}"},
            harness="fixture")
        docs["spec"] = f"{READOUT}:{spec.id}"
        docs["stop"] = _doc(READOUT, _append(
            lg, "item.stopped", READOUT,
            {"point": f"paused at {CHILD}", "phase": "build"}, "stop"))
        docs["recorded"] = _doc(READOUT, _append(
            lg, "decision.recorded", READOUT,
            {"who": "writer-a", "who_kind": "person", "standing": "lean",
             "effect": f"{CHILD} cites its source by line", "verbatim": "by line",
             "tags": [{"type": "initiative", "id": "INIT-ALPHA"}],
             "phase": "plan"}, "recorded"))
        docs["utterance"] = "event:%d" % _append(
            lg, "utterance.captured", BYSTANDER,
            {"text": f"about {CHILD}", "origin": "person"}, "said").id
        # The child's own notes never count as mentions of itself.
        docs["self-ruling"] = _doc(CHILD, _append(
            lg, "item.note", CHILD, {"text": f"decided: {CHILD} keeps its id"}, "self-ruling"))
        docs["self-stop"] = _doc(CHILD, _append(
            lg, "item.stopped", CHILD,
            {"point": f"resume {CHILD}", "phase": "build"}, "self-stop"))
        # A sibling whose own ruling names the sibling, never the child.
        docs["sibling-ruling"] = _doc(SIBLING, _append(
            lg, "item.note", SIBLING,
            {"text": f"decided: {SIBLING} follows its own rules"}, "sibling-ruling"))
    _config(db)
    return {"db": db, "docs": docs}


def _mention_ids(monkeypatch, db: Path, *argv: str) -> list[str]:
    code, body = run_json(monkeypatch, db, "search", "--mentions", *argv)
    assert code == EXIT_OK, body
    return [card["id"] for card in body["data"]["cards"]]


# --- 1. --mentions: ranked, decision documents first --------------------------


def test_mentions_returns_the_parents_decided_note_first(ledger, monkeypatch):
    ids = _mention_ids(monkeypatch, ledger["db"], CHILD)
    assert ids[0] == ledger["docs"]["ruling"], ids
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", CHILD)
    first = body["data"]["cards"][0]
    assert (first["doc_type"], first["item_id"]) == ("decision", PARENT)


def test_decision_documents_come_before_the_rest_which_keep_rank_order(ledger, monkeypatch):
    docs = ledger["docs"]
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", CHILD)
    cards = body["data"]["cards"]
    kinds = [card["doc_type"] for card in cards]
    assert set(kinds) == {"decision", "spec", "stop", "utterance"}
    decisions = kinds.count("decision")
    assert decisions == 2
    assert set(kinds[:decisions]) == {"decision"}
    assert "decision" not in kinds[decisions:]
    assert {card["id"] for card in cards[:decisions]} == {docs["ruling"], docs["recorded"]}
    # The rest are in the order a normal search ranks them.
    rest = [card["id"] for card in cards[decisions:]]
    plain = _ids(monkeypatch, ledger["db"], CHILD, "--limit", str(MAX_LIMIT))
    assert rest == [doc_id for doc_id in plain if doc_id in rest]
    # Among decisions a decided: note comes before the others, though a
    # recorded decision outranks it on score.
    assert plain.index(docs["recorded"]) < plain.index(docs["ruling"])
    assert [card["id"] for card in cards[:decisions]] == [docs["ruling"], docs["recorded"]]


def test_the_fixture_ranks_a_non_decision_above_the_ruling_by_score(ledger, monkeypatch):
    # Without the decision-first rule the ruling would not lead; this is what
    # makes the ordering test above guard something.
    plain = _ids(monkeypatch, ledger["db"], CHILD, "--limit", str(MAX_LIMIT))
    docs = ledger["docs"]
    assert plain.index(docs["spec"]) < plain.index(docs["ruling"])
    assert plain.index(docs["stop"]) < plain.index(docs["ruling"])


def test_an_item_document_is_never_a_mention(ledger, monkeypatch):
    # The parent's plain note names the child, but only inside the parent's
    # item document, which names no single event.
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", CHILD)
    assert PARENT not in [card["id"] for card in body["data"]["cards"]]
    assert "item" not in {card["doc_type"] for card in body["data"]["cards"]}


def test_an_item_never_mentions_itself(ledger, monkeypatch):
    docs = ledger["docs"]
    ids = _mention_ids(monkeypatch, ledger["db"], CHILD)
    assert docs["self-ruling"] not in ids
    assert docs["self-stop"] not in ids
    assert {doc_id.split(":")[0] for doc_id in ids if not doc_id.startswith("event:")} == {
        PARENT, READOUT}


def test_a_document_with_no_item_is_not_on_another_item(ledger, monkeypatch, tmp_path):
    root = tmp_path / "notes"
    (root / "standards").mkdir(parents=True)
    (root / "standards" / "map.md").write_text(
        f"# Map\n\n## Owner\nThe map belongs to {CHILD}.\n", encoding="utf-8")
    _config(ledger["db"], [root])
    code, body = run_json(monkeypatch, ledger["db"], "search", "belongs", "--type", "standard")
    assert code == EXIT_OK and len(body["data"]["cards"]) == 1  # indexed, and names it
    ids = _mention_ids(monkeypatch, ledger["db"], CHILD)
    assert not any(doc_id.startswith("standards/") for doc_id in ids)


# --- 2. the whole-token grammar -----------------------------------------------

# Each near miss is a decided: note on another item: a decision document the
# ranking would put first, were it not dropped. ``candidate`` says the index
# match itself finds it, so only the whole-token check drops it.
NEAR_MISSES = [
    ("longer-id-hyphen-digit", f"decided: {CHILD}-2 is the follow-up", True),
    ("longer-id-digit", f"decided: {CHILD}2 is the follow-up", False),
    ("longer-id-word", f"decided: {CHILD}-extra is the follow-up", True),
    ("plural", f"decided: {CHILD}s are the follow-up", True),
    ("prefixed-word", f"decided: x{CHILD} is a typo", False),
    ("prefixed-hyphen", f"decided: parent-{CHILD} is another id", True),
    ("other-case", "decided: Child-Item differs by case", True),
    ("underscore", "decided: child_item is not it", True),
    ("space", "decided: child item is two words", True),
    ("dotted-continuation", f"decided: {CHILD}.notes are generated", True),
    ("dotted-prefix", f"decided: v1.{CHILD} is a version", True),
]
NAMES_IT = [
    ("bare", f"decided: {CHILD}"),
    ("sentence-end", f"decided: close out {CHILD}."),
    ("comma", f"decided: {CHILD}, then the rest"),
    ("parenthesis", f"decided: close it (see {CHILD}) now"),
    ("semicolon", f"decided: {CHILD}; its tooltips are attested"),
    ("quoted", f"decided: the \"{CHILD}\" ticket"),
    ("middle", f"decided: owned by {CHILD} and nobody else"),
    ("after-colon", f"decided:{CHILD}"),
]


def _two_item_ledger(tmp_path: Path, monkeypatch, note: str) -> tuple[Path, str]:
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    db = tmp_path / "state" / "home.db"
    with open_ledger(db) as lg:
        _append(lg, "item.created", CHILD, {"type": "ticket", "title": "Map tooltips"}, "child")
        _append(lg, "item.created", BYSTANDER,
                {"type": "ticket", "title": "Unrelated chores"}, "bystander")
        event = _append(lg, "item.note", BYSTANDER, {"text": note}, "note")
    _config(db)
    return db, f"{BYSTANDER}:{event.id}"


@pytest.mark.parametrize("name,note,candidate", NEAR_MISSES, ids=[n[0] for n in NEAR_MISSES])
def test_a_near_miss_of_the_id_is_not_a_mention(tmp_path, monkeypatch, name, note, candidate):
    db, doc_id = _two_item_ledger(tmp_path, monkeypatch, note)
    if candidate:
        # The index match alone finds it; only the whole-token check drops it.
        assert doc_id in _ids(monkeypatch, db, "--type", "decision", CHILD)
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(db), "search", "--mentions", CHILD])
    assert "Traceback" not in err
    body = json.loads(out)
    assert code == EXIT_NOT_FOUND, body
    assert body["error"]["code"] == "empty"


@pytest.mark.parametrize("name,note", NAMES_IT, ids=[n[0] for n in NAMES_IT])
def test_an_id_between_non_id_characters_is_a_mention(tmp_path, monkeypatch, name, note):
    db, doc_id = _two_item_ledger(tmp_path, monkeypatch, note)
    assert _mention_ids(monkeypatch, db, CHILD) == [doc_id]


@pytest.mark.parametrize("text,expected", [
    (f"see {CHILD}", True),
    (f"{CHILD}", True),
    (f"{CHILD}-2", False),
    (f"x{CHILD}", False),
    (f"{CHILD}2", False),
    (f"{CHILD}_2", False),
    (f"2_{CHILD}", False),
    (f"a.{CHILD}", False),
    (f"{CHILD}.b", False),
    (f"{CHILD}. B", True),
    (f"{CHILD}.", True),
    (".child-item", True),
    ("Child-Item", False),
    ("", False),
    (None, False),
])
def test_names_item_is_a_whole_token_match(text, expected):
    assert search_mod.names_item(text, CHILD) is expected


def test_the_id_is_compared_exactly_as_stored():
    assert search_mod.names_item("see ID-7", "ID-7") is True
    assert search_mod.names_item("see id-7", "ID-7") is False
    assert search_mod.names_item("see ID-7", "id-7") is False
    # An empty id names nothing, even where the boundary rules alone would match.
    assert search_mod.names_item("a  b", "") is False


def test_a_sibling_id_is_not_a_mention_of_the_child(ledger, monkeypatch):
    docs = ledger["docs"]
    assert docs["sibling-ruling"] not in _mention_ids(monkeypatch, ledger["db"], CHILD)
    # And the child is not a mention of the sibling: its ruling names its own id.
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", SIBLING)
    assert code == EXIT_NOT_FOUND, body


# --- 3. an id that names no item is a usage refusal ---------------------------


@pytest.mark.parametrize("given", ["no-such-item", "Child-Item", " child-item", "child-item ", "", "child"])
def test_an_id_that_names_no_item_is_a_usage_error_naming_it(ledger, monkeypatch, given):
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", str(ledger["db"]), "search", "--mentions", given])
    assert "Traceback" not in err
    body = json.loads(out)
    assert code == EXIT_USAGE == 2, body
    assert body["ok"] is False
    assert body["error"]["code"] == "usage"
    assert repr(given) in body["error"]["message"]


def test_a_known_item_with_no_mentions_is_not_the_unknown_refusal(ledger, monkeypatch):
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", BYSTANDER)
    assert code == EXIT_NOT_FOUND, body
    assert body["error"]["code"] == "empty"
    assert BYSTANDER in body["error"]["message"]


def test_the_library_raises_unknown_item(ledger):
    with pytest.raises(UnknownItem, match="no item named 'typo-item'"):
        search_mod.mentions(ledger["db"], "typo-item", limit=10)
    # An unknown item is a search error to a caller that does not tell the two apart.
    assert issubclass(UnknownItem, search_mod.SearchError)
    with pytest.raises(search_mod.SearchError, match="must be a string"):
        search_mod.mentions(ledger["db"], 7, limit=10)


# --- 4. --limit, --type and the other filters like a normal search -------------


def test_limit_cuts_the_list_and_says_so(ledger, monkeypatch):
    full = _mention_ids(monkeypatch, ledger["db"], CHILD)
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", CHILD, "--limit", "2")
    assert code == EXIT_OK
    assert [card["id"] for card in body["data"]["cards"]] == full[:2]
    assert body["data"]["truncated"] is True and body["data"]["limit"] == 2
    code, body = run_json(
        monkeypatch, ledger["db"], "search", "--mentions", CHILD, "--limit", str(len(full)))
    assert body["data"]["truncated"] is False
    code, text, err = invoke(
        monkeypatch, ["--db", str(ledger["db"]), "search", "--mentions", CHILD, "--limit", "2"])
    assert code == EXIT_OK
    assert text.rstrip("\n").splitlines()[-1].startswith("+more ")


@pytest.mark.parametrize("flag", ["0", str(MAX_LIMIT + 1)])
def test_a_limit_out_of_range_is_refused(ledger, monkeypatch, flag):
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", CHILD, "--limit", flag)
    assert code == EXIT_USAGE, body


def test_type_narrows_the_mentions(ledger, monkeypatch):
    docs = ledger["docs"]
    assert set(_mention_ids(monkeypatch, ledger["db"], CHILD, "--type", "decision")) == {
        docs["ruling"], docs["recorded"]}
    assert _mention_ids(monkeypatch, ledger["db"], CHILD, "--type", "spec") == [docs["spec"]]
    assert _mention_ids(monkeypatch, ledger["db"], CHILD, "--type", "stop") == [docs["stop"]]
    assert _mention_ids(monkeypatch, ledger["db"], CHILD, "--type", "utterance") == [docs["utterance"]]
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", CHILD, "--type", "item")
    assert code == EXIT_NOT_FOUND, body


def test_item_and_status_filters_apply(ledger, monkeypatch):
    docs = ledger["docs"]
    assert set(_mention_ids(monkeypatch, ledger["db"], CHILD, "--item", READOUT)) == {
        docs["spec"], docs["stop"], docs["recorded"]}
    assert _mention_ids(monkeypatch, ledger["db"], CHILD, "--item", PARENT) == [docs["ruling"]]
    code, body = run_json(
        monkeypatch, ledger["db"], "search", "--mentions", CHILD, "--status", "closed")
    assert code == EXIT_NOT_FOUND, body


def test_json_filters_carry_the_id_and_the_filters_given(ledger, monkeypatch):
    code, body = run_json(
        monkeypatch, ledger["db"], "search", "--mentions", CHILD, "--type", "decision",
        "--item", PARENT)
    assert code == EXIT_OK, body
    assert body["data"]["filters"] == {"mentions": CHILD, "type": "decision", "item": PARENT}


def test_json_has_the_normal_shape_and_names_the_id(ledger, monkeypatch):
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", CHILD)
    mentions_data = body["data"]
    code, normal = run_json(monkeypatch, ledger["db"], "search", NORMAL_WORD)
    assert set(mentions_data) == set(normal["data"])
    assert mentions_data["query"] == CHILD
    assert mentions_data["filters"] == {"mentions": CHILD}
    assert mentions_data["ranker"] == "bm25"
    for card in mentions_data["cards"]:
        assert {"score", "doc_type", "id", "title", "snippet", "item_id"} <= set(card)
    utterance = [c for c in mentions_data["cards"] if c["doc_type"] == "utterance"][0]
    assert utterance.get("caveat") == search_mod.UTTERANCE_CAVEAT
    assert mentions_data["filters"] == {"mentions": CHILD}


def test_text_has_the_normal_shape(ledger, monkeypatch):
    code, text, err = invoke(monkeypatch, ["--db", str(ledger["db"]), "search", "--mentions", CHILD])
    assert code == EXIT_OK and err == ""
    lines = text.splitlines()
    first = lines[0].split("\t")
    assert len(first) == 4 and first[1] == "decision" and first[2] == ledger["docs"]["ruling"]
    assert lines[1].startswith("\t") and CHILD in lines[1]
    assert not any("mentioned in:" in line for line in lines)


def test_words_and_mentions_together_are_refused(ledger, monkeypatch):
    code, body = run_json(monkeypatch, ledger["db"], "search", "--mentions", CHILD, "tooltips")
    assert code == EXIT_USAGE, body
    assert "no search words" in body["error"]["message"]
    code, body = run_json(monkeypatch, ledger["db"], "search", "tooltips", "--mentions", CHILD)
    assert code == EXIT_USAGE, body


def test_neither_words_nor_mentions_is_refused(ledger, monkeypatch):
    code, body = run_json(monkeypatch, ledger["db"], "search")
    assert code == EXIT_USAGE, body
    assert "--mentions" in body["error"]["message"]
    code, body = run_json(monkeypatch, ledger["db"], "search", "--limit", "3")
    assert code == EXIT_USAGE, body


def test_a_stale_index_is_brought_up_to_date_before_mentions_reads_it(ledger, monkeypatch):
    db = ledger["db"]
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    with open_ledger(db) as lg:
        late = _append(lg, "item.note", BYSTANDER, {"text": f"decided: late word on {CHILD}"}, "late")
        before = [event.hash for event in lg.events()]
    assert search_mod.index_status(db)["status"] == "behind"
    # A caller that must not rewrite the index is refused, never answered stale.
    refused = _error(IndexNotCurrent, search_mod.mentions, db, CHILD, limit=10, refresh=False)
    assert "behind" in str(refused)
    ids = _mention_ids(monkeypatch, db, CHILD)
    assert f"{BYSTANDER}:{late.id}" in ids
    with open_ledger(db) as lg:
        assert [event.hash for event in lg.events()] == before


# --- 5. the mentioned-in line on an item hit ----------------------------------


def _text_search(monkeypatch, db: Path, *argv: str) -> list[str]:
    code, text, err = invoke(monkeypatch, ["--db", str(db), "search", *argv])
    assert code == EXIT_OK and "Traceback" not in err, err
    return text.splitlines()


def _ruling_event(ledger: dict) -> int:
    return int(ledger["docs"]["ruling"].split(":")[1])


def test_an_item_hit_prints_its_mentioned_in_lines_decided_note_first(ledger, monkeypatch):
    lines = _text_search(monkeypatch, ledger["db"], "--type", "item", NORMAL_WORD, "--limit", "20")
    at = next(i for i, line in enumerate(lines) if line.split("\t")[2:3] == [CHILD])
    assert lines[at + 1].startswith("\t") and not lines[at + 1].startswith("\tmentioned in")
    # Two lines, no more: the child has four other mentions.
    mention_lines = []
    for line in lines[at + 2:]:
        if not line.startswith("\tmentioned in: "):
            break
        mention_lines.append(line)
    assert len(mention_lines) == 2, lines[at:at + 6]
    assert mention_lines[0] == (
        f"\tmentioned in: {PARENT} event={_ruling_event(ledger)} {RULING[:80]}")
    assert mention_lines[1].startswith(f"\tmentioned in: {READOUT} event=")
    # The second is the other decision document (the recorded decision).
    recorded = int(ledger["docs"]["recorded"].split(":")[1])
    assert mention_lines[1].startswith(f"\tmentioned in: {READOUT} event={recorded} ")


def test_an_item_hit_with_no_mention_prints_no_line(ledger, monkeypatch):
    lines = _text_search(monkeypatch, ledger["db"], "--type", "item", "chores")
    assert any(line.split("\t")[2:3] == [BYSTANDER] for line in lines)
    assert not any("mentioned in:" in line for line in lines)


def test_the_line_cuts_the_text_at_80_characters(ledger, monkeypatch):
    lines = _text_search(monkeypatch, ledger["db"], "--type", "item", NORMAL_WORD)
    line = next(line for line in lines if line.startswith("\tmentioned in: " + PARENT))
    assert len(RULING) > 80
    assert line.endswith(RULING[:80]) and RULING[:81] not in line


def test_only_an_item_hit_gets_the_line(ledger, monkeypatch):
    lines = _text_search(monkeypatch, ledger["db"], "--type", "decision", "belongs", "nothing")
    assert any(line.split("\t")[1:2] == ["decision"] for line in lines)
    assert not any("mentioned in:" in line for line in lines)


def test_a_mention_text_is_escaped_like_other_stored_text():
    line = search_verb._mention_line(Mention("x\ny", 5, "a\x1b[2Jb\nc\td", "decision"))
    assert line == "mentioned in: x\\ny event=5 a\\u001b[2Jb\\nc\\td"
    assert "\x1b" not in line and "\n" not in line and "\t" not in line


def test_a_mention_without_an_event_number_prints_no_line(ledger, monkeypatch):
    monkeypatch.setattr(search_mod, "_event_number", lambda doc_id: None)
    lines = _text_search(monkeypatch, ledger["db"], "--type", "item", NORMAL_WORD)
    assert not any("mentioned in:" in line for line in lines)


@pytest.mark.parametrize("doc_id,expected", [
    ("parent-item:12", 12), ("event:7", 7), ("a:b:30", 30),
    ("parent-item", None), ("parent-item:", None), (":5", None),
    ("parent-item:\u0661\u0662", None),
    ("parent-item:x", None), ("parent-item:1.5", None), ("parent-item:-3", None),
])
def test_the_event_number_is_the_id_after_the_last_colon(doc_id, expected):
    assert search_mod._event_number(doc_id) == expected


# --- 6. a normal search is unchanged ------------------------------------------


def test_normal_json_is_the_search_result_byte_for_byte(ledger, monkeypatch):
    db = ledger["db"]
    run_json(monkeypatch, db, "index")
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(db), "search", NORMAL_WORD])
    assert code == EXIT_OK
    expected = search_mod.search(db, NORMAL_WORD, limit=10)
    assert expected["cards"], "the fixture must hit an item with mentions"
    assert out == json.dumps({"data": expected, "error": None, "ok": True}, sort_keys=True) + "\n"
    assert "mention" not in out


@pytest.mark.parametrize("argv", [
    (NORMAL_WORD,), ("--type", "item", NORMAL_WORD), (CHILD,), ("--limit", "2", NORMAL_WORD),
])
def test_normal_text_differs_only_by_the_mentioned_in_lines(ledger, monkeypatch, argv):
    db = ledger["db"]
    run_json(monkeypatch, db, "index")
    lines = _text_search(monkeypatch, db, *argv)
    shown = [line for line in lines if not line.startswith("\tmentioned in: ")]
    code, body = run_json(monkeypatch, db, "search", *argv)
    data = body["data"]
    assert shown == search_verb._text(data["cards"], truncated=data["truncated"]).splitlines()
    # And the cards are the ones the library returns, in its order.
    expected = search_mod.search(db, " ".join(argv[-1:]), limit=data["limit"],
                                 doc_type=data["filters"].get("type"))
    assert data["cards"] == expected["cards"]


def test_search_mentions_writes_no_event(ledger, monkeypatch):
    db = ledger["db"]
    with open_ledger(db) as lg:
        before = [event.hash for event in lg.events()]
    _mention_ids(monkeypatch, db, CHILD)
    _text_search(monkeypatch, db, NORMAL_WORD)
    with open_ledger(db) as lg:
        assert [event.hash for event in lg.events()] == before


# --- 7. the library's own guards ----------------------------------------------


@pytest.mark.parametrize("options,message", [
    ({"limit": 0}, "limit must be an integer"),
    ({"limit": MAX_LIMIT + 1}, "limit must be an integer"),
    ({"limit": 10, "status": "bogus"}, "status must be one of"),
    ({"limit": 10, "since": datetime(2026, 1, 1)}, "since must be a timezone-aware datetime"),
])
def test_the_library_checks_its_options_like_search(ledger, options, message):
    refused = _error(search_mod.SearchError, search_mod.mentions, ledger["db"], CHILD, **options)
    assert message in str(refused)


def test_item_mentions_brings_a_stale_index_up_to_date(ledger, monkeypatch):
    db = ledger["db"]
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        late = _append(lg, "item.note", BYSTANDER, {"text": f"decided: late word on {SIBLING}"}, "late")
    assert search_mod.index_status(db)["status"] == "behind"
    try:
        found = search_mod.item_mentions(db, [SIBLING, SIBLING], per_item=2)
    except IndexNotCurrent as exc:
        raise AssertionError(f"item_mentions did not refresh the index: {exc}") from exc
    assert list(found) == [SIBLING]
    assert [(m.item_id, m.event, m.doc_type) for m in found[SIBLING]] == [
        (BYSTANDER, late.id, "decision")]


def test_item_mentions_takes_no_ids_and_cuts_at_per_item(ledger):
    assert search_mod.item_mentions(ledger["db"], []) == {}
    found = search_mod.item_mentions(ledger["db"], [CHILD], per_item=3)
    assert len(found[CHILD]) == 3
    assert search_mod.MENTION_LINES == 2
    assert len(search_mod.item_mentions(ledger["db"], [CHILD])[CHILD]) == 2


def test_json_search_does_not_look_up_mentions(ledger, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("JSON output must not read mentions")

    monkeypatch.setattr(search_verb, "item_mentions", boom)
    code, body = run_json(monkeypatch, ledger["db"], "search", "--type", "item", NORMAL_WORD)
    assert code == EXIT_OK, body
    assert CHILD in [card["id"] for card in body["data"]["cards"]]


def test_a_decision_hit_whose_id_is_named_in_a_note_gets_no_line(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    db = tmp_path / "state" / "home.db"
    with open_ledger(db) as lg:
        _append(lg, "item.created", "alpha-item", {"type": "ticket", "title": "Alpha"}, "alpha")
        _append(lg, "item.created", "beta-item", {"type": "ticket", "title": "Beta"}, "beta")
        ruling = _append(lg, "item.note", "alpha-item", {"text": "decided: use the quartz store"}, "r")
        _append(lg, "item.note", "beta-item",
                {"text": f"decided: follow alpha-item:{ruling.id}"}, "r2")
    _config(db)
    lines = _text_search(monkeypatch, db, "--type", "decision", "quartz")
    assert lines[0].split("\t")[2] == f"alpha-item:{ruling.id}"
    assert not any("mentioned in:" in line for line in lines)


def test_an_id_with_no_searchable_word_is_refused_by_name_never_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    db = tmp_path / "state" / "home.db"
    with open_ledger(db) as lg:
        _append(lg, "item.created", "!!!", {"type": "ticket", "title": "Punctuation only"}, "p")
        _append(lg, "item.note", "!!!", {"text": "decided: it is all marks"}, "pn")
    _config(db)
    code, body = run_json(monkeypatch, db, "search", "--mentions", "!!!")
    assert code != EXIT_OK and code != EXIT_NOT_FOUND, body
    assert "no searchable words" in body["error"]["message"]
    # A hit on that item still prints, with no mentioned-in line and no failure.
    lines = _text_search(monkeypatch, db, "--type", "item", "punctuation")
    assert lines[0].split("\t")[2] == "!!!"
    assert not any("mentioned in:" in line for line in lines)
