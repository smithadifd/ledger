"""Stored item text reaches the terminal escaped, and new item text that can
drive a terminal is refused at write.

Render side: ``ledger card`` (text and ``--md``), ``spec show`` and ``spec
diff``, ``export-item``, ``context``, ``search`` and the item listings
(``query``, ``ready``) print stored text through ``_one_line`` (one-line
fields) or ``_multi_line`` (Markdown: LF and TAB kept). Both escapers write
every other control character, the Unicode line and paragraph separators and
the bidirectional controls as visible escapes. ``--json`` forms keep the
stored text; ``json.dumps`` escapes it.

Write side: an ``item.created`` or ``item.spec`` title refuses every Cc, the
line and paragraph separators and the bidirectional controls; note, progress,
spec body and acceptance refuse the same set less LF and TAB. The refusal is
``display_control_character:<kind>.<field>``. It is a write-time rule only:
rows written before it still verify, export, restore and print, escaped.

Every control and bidirectional code point in this file is spelled as an
escape, never raw.
"""
from __future__ import annotations

import itertools
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from ledger.card import (
    FOOTER,
    _multi_line,
    _one_line,
    blockers_command,
    children_command,
    derive_next_step,
    duplicates_command,
    footer_line,
    format_card,
    project_card,
    recover_command,
)
from ledger.export import export_events, verify_export
from ledger.restore import replay_events, restore_export
from ledger.schema import ValidationError
from ledger.store import canonical_dumps, open_ledger
from ledger.verbs.spec import set_spec

from test_election_binding import run_cli

ITEM = "esc-1"
ESC = "\x1b"
BEL = "\x07"
CSI_C1 = "\x9b"
OSC = ESC + "]52;c;aGk=" + BEL
RLO = "\u202e"
LRI = "\u2066"
LS = "\u2028"
PS = "\u2029"
NEL = "\x85"
BIDI = tuple(chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
_keys = itertools.count(1)


# --- helpers ------------------------------------------------------------------


@contextmanager
def pre_rule():
    """Writes as an older version stored them, before the write-time rule:
    the one guard is off for these writes, so each row, its hash chain and
    its schema are exactly what that version stored."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "ledger.store.refuse_display_control_characters", lambda kind, payload: None
        )
        yield


def add(lg, kind: str, payload: dict, item_id: str = ITEM, key: str | None = None):
    return lg.append(
        kind=kind,
        payload=payload,
        item_id=item_id,
        idempotency_key=key or f"k-{next(_keys)}",
        harness="pytest",
    )


def item(lg, item_id: str = ITEM, title: str = "an item", item_type: str = "ticket"):
    return add(lg, "item.created", {"type": item_type, "title": title}, item_id)


def spec(lg, item_id: str = ITEM, **content):
    content.setdefault("title", "a spec")
    content.setdefault("body_md", "a body\n")
    event, created = set_spec(lg, item_id, content, harness="pytest")
    assert created
    return event


def text_out(monkeypatch, db, *argv: str) -> str:
    code, out, err = run_cli(monkeypatch, ["--db", str(db), *argv])
    assert code == 0, (out, err)
    return out


def json_out(monkeypatch, db, *argv: str) -> tuple[str, dict]:
    code, out, err = run_cli(monkeypatch, ["--json", "--db", str(db), *argv])
    assert code == 0, (out, err)
    return out, json.loads(out)["data"]


def refused(monkeypatch, db, *argv: str, stdin: str | None = None) -> str:
    """The message of a validation refusal from the CLI (exit 3)."""
    code, out, err = run_cli(monkeypatch, ["--json", "--db", str(db), *argv], stdin=stdin)
    assert code == 3, (out, err)
    error = json.loads(out)["error"]
    assert error["code"] == "validation", error
    return error["message"]


def lines(text: str) -> list[str]:
    # Printed text holds no raw separator once escaped; split on LF only.
    return text.split("\n")


def assert_no_raw(text: str, *chars: str) -> None:
    for char in chars:
        assert char not in text, f"raw U+{ord(char):04X} in output"


def event_count(db) -> int:
    with open_ledger(db, create=False) as lg:
        return len(lg.events())


# --- T1, T2, T3: card notes, progress and title ---------------------------------


@pytest.mark.parametrize("full", [False, True], ids=["bounded", "full"])
def test_a_note_with_an_osc52_clipboard_write_prints_escaped_on_one_line(
        tmp_path, monkeypatch, full) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg)
        add(lg, "item.note", {"text": "a" + OSC + "b\nc"})
    md = tmp_path / "card.md"
    out = text_out(monkeypatch, db, "card", *(["--full"] if full else []), "--md", str(md), ITEM)
    written = md.read_bytes().decode("utf-8")
    for shown in (out, written):
        assert_no_raw(shown, ESC, BEL)
        assert any(
            line.endswith(": " + r"a\u001b]52;c;aGk=\u0007b\nc") for line in lines(shown)
        ), shown


@pytest.mark.parametrize("full", [False, True], ids=["bounded", "full"])
def test_a_progress_row_with_a_clear_screen_prints_escaped(tmp_path, monkeypatch, full) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg)
        add(lg, "item.progress", {"text": ESC + "[2Jx"})
    md = tmp_path / "card.md"
    out = text_out(monkeypatch, db, "card", *(["--full"] if full else []), "--md", str(md), ITEM)
    written = md.read_bytes().decode("utf-8")
    for shown in (out, written):
        assert_no_raw(shown, ESC)
        assert "## Progress" in shown
        assert r"\u001b[2Jx" in shown


def test_the_title_line_prints_escaped_on_one_line(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg, title="T" + ESC + "[31m\nX")
    out = text_out(monkeypatch, db, "card", ITEM)
    assert_no_raw(out, ESC)
    assert "title: " + r"T\u001b[31m\nX" in lines(out)
    assert "X" not in lines(out)


# --- T4: every other card field a write can carry -------------------------------

V = "v" + ESC + "[2J"
V_SHOWN = r"v\u001b[2J"
OPTIONS = [{"label": "a", "description": "first"}, {"label": "b", "description": "second"}]
RECORDED = {
    "who": "tester",
    "who_kind": "person",
    "standing": "lean",
    "effect": "an effect",
    "tags": [{"type": "ticket", "id": ITEM}],
    "verbatim": "some words",
    "phase": "build",
}
ELECTED = {
    "question": "which?",
    "chosen": "blue",
    "chosen_description": "",
    "rejected": [],
    "scope": "palette",
    "quote_ref": 1,
}


def _asked(**changes) -> dict:
    payload = {"question": "which?", "options": [dict(o) for o in OPTIONS]}
    payload.update(changes)
    return payload


def _option(index: int, name: str) -> dict:
    options = [dict(o) for o in OPTIONS]
    options[index][name] = V
    return _asked(options=options)


def _record(lg, **changes) -> None:
    add(lg, "decision.recorded", {**RECORDED, **changes})


def _result(lg) -> None:
    decision = add(lg, "decision.recorded", dict(RECORDED))
    add(lg, "decision.result", {"decision_event_id": decision.id, "result": V})


# (field, item type, writer); a writer stores V in that one field.
CARD_FIELDS = [
    ("owner", "ticket", lambda lg: add(lg, "item.state", {"status": "open", "owner": V})),
    ("claim", "ticket", lambda lg: add(lg, "item.state", {"status": "claimed", "claim": V})),
    ("blocker", "ticket",
     lambda lg: add(lg, "item.state", {"status": "blocked", "blocked_by": [V]})),
    ("link-target", "ticket", lambda lg: add(lg, "item.linked", {"rel": "url", "target": V})),
    ("elected-question", "ticket",
     lambda lg: add(lg, "decision.elected", {**ELECTED, "question": V})),
    ("elected-chosen", "decision",
     lambda lg: add(lg, "decision.elected", {**ELECTED, "chosen": V})),
    ("ask-question", "ticket", lambda lg: add(lg, "decision.asked", _asked(question=V))),
    ("ask-ask", "ticket", lambda lg: add(lg, "decision.asked", _asked(ask=V))),
    ("ask-recommendation", "ticket",
     lambda lg: add(lg, "decision.asked", _asked(recommendation=V))),
    ("ask-default", "ticket", lambda lg: add(lg, "decision.asked", _asked(default=V))),
    ("ask-effort", "ticket", lambda lg: add(lg, "decision.asked", _asked(effort=V))),
    ("ask-added", "ticket", lambda lg: add(lg, "decision.asked", _asked(added=V))),
    ("ask-unblocks", "ticket", lambda lg: add(lg, "decision.asked", _asked(unblocks=V))),
    ("option-label", "ticket", lambda lg: add(lg, "decision.asked", _option(0, "label"))),
    ("option-description", "ticket",
     lambda lg: add(lg, "decision.asked", _option(1, "description"))),
    ("record-who", "ticket", lambda lg: _record(lg, who=V)),
    ("record-relayed-by", "ticket", lambda lg: _record(lg, who_kind="role", relayed_by=V)),
    ("record-effect", "ticket", lambda lg: _record(lg, effect=V)),
    ("record-verbatim", "ticket", lambda lg: _record(lg, verbatim=V)),
    ("record-tag-id", "ticket", lambda lg: _record(lg, tags=[{"type": "ticket", "id": V}])),
    ("record-evidence", "ticket", lambda lg: _record(lg, evidence=V)),
    ("record-result", "ticket", _result),
    ("tried-what", "ticket", lambda lg: add(lg, "item.tried", {"what": V})),
    ("tried-outcome", "ticket",
     lambda lg: add(lg, "item.tried", {"what": "a try", "outcome": V})),
    ("review-finding", "ticket",
     lambda lg: add(lg, "review.recorded", {"round": 1, "verdict": "changes", "findings": [V]})),
    ("stop-point", "ticket",
     lambda lg: add(lg, "item.stopped", {"point": V, "phase": "build"})),
]


@pytest.mark.parametrize(
    "item_type,write", [pytest.param(t, w, id=f) for f, t, w in CARD_FIELDS]
)
def test_every_card_field_from_stored_text_prints_escaped(tmp_path, item_type, write) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg, item_type=item_type)
        write(lg)
    with open_ledger(db, create=False) as lg:
        for full in (False, True):
            shown = format_card(project_card(lg, ITEM, full=full))
            assert_no_raw(shown, ESC)
            assert V_SHOWN in shown


@pytest.mark.parametrize("name", ["ref", "note"])
def test_a_pointer_from_the_sidecar_prints_escaped(tmp_path, name) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        item(lg)
        entry = {"ref": "notes/a.md", "note": "a note", name: V}
        shown = format_card(project_card(lg, ITEM, pointers=lambda _item: [entry]))
    assert_no_raw(shown, ESC)
    assert V_SHOWN in shown


# --- T5, T6: spec show and the escapers -----------------------------------------


def test_spec_show_escapes_the_body_and_keeps_its_lines(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg)
        spec(
            lg,
            title="S" + ESC + "[2J",
            acceptance="ok" + ESC + "[2J\nnext",
            body_md="# H\n\n\tcode" + ESC + "[2J\r\nz" + LS + "w\n",
        )
    out = text_out(monkeypatch, db, "spec", "show", ITEM)
    assert_no_raw(out, ESC, LS)
    shown = lines(out)
    assert "title: " + r"S\u001b[2J" in shown
    body = shown.index("## Body")
    assert shown[body + 1:body + 5] == ["# H", "", "\tcode" + r"\u001b[2J\r", r"z\u2028w"]
    acceptance = shown.index("## Acceptance")
    assert shown[acceptance + 1:acceptance + 3] == [r"ok\u001b[2J", "next"]


ESCAPED_CODE_POINTS = [
    *range(0x00, 0xA0), 0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A)
]


def test_the_escapers_write_every_listed_code_point_visibly() -> None:
    named = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}
    for code in ESCAPED_CODE_POINTS:
        char = chr(code)
        printable = 0x20 <= code < 0x7F
        if printable:
            assert _one_line(char) == char
            assert _multi_line(char) == char
            continue
        expected = named.get(char, f"\\u{code:04x}")
        assert _one_line(char) == expected, hex(code)
        if char in ("\n", "\t"):
            assert _multi_line(char) == char
        else:
            assert _multi_line(char) == expected, hex(code)
    listed = [chr(code) for code in ESCAPED_CODE_POINTS if not 0x20 <= code < 0x7F]
    every = "".join(listed)
    assert not any(char in _one_line(every) for char in listed)
    assert not any(char in _multi_line(every) for char in listed if char not in "\n\t")
    for kept in ("\u00e9", "\u200d", "\u200e"):
        assert _one_line(kept) == kept
        assert _multi_line(kept) == kept


# --- T7, T7b, T8: the write rule ---------------------------------------------------

MULTI_REFUSED = [
    pytest.param(ESC, id="ESC"), pytest.param(BEL, id="BEL"), pytest.param("\x00", id="NUL"),
    pytest.param("\r", id="CR"), pytest.param("\x0b", id="VT"), pytest.param("\x7f", id="DEL"),
    pytest.param(NEL, id="U+0085"), pytest.param(CSI_C1, id="U+009B"),
    pytest.param(LS, id="U+2028"), pytest.param(PS, id="U+2029"),
    pytest.param("\u202a", id="U+202A"), pytest.param(RLO, id="U+202E"),
    pytest.param(LRI, id="U+2066"), pytest.param("\u2069", id="U+2069"),
]
ONE_LINE_REFUSED = [
    *MULTI_REFUSED, pytest.param("\n", id="LF"), pytest.param("\t", id="TAB"),
]


def _cli_note(monkeypatch, db, text):
    return refused(monkeypatch, db, "note", ITEM, "--text", text)


def _cli_append(kind, field):
    def write(monkeypatch, db, text):
        payload = {field: text}
        return refused(monkeypatch, db, "append", "--kind", kind, "--item", ITEM,
                       "--payload", json.dumps(payload))
    return write


def _cli_append_many(kind, field):
    def write(monkeypatch, db, text):
        batch = [{"kind": kind, "payload": {field: text}}]
        return refused(monkeypatch, db, "append-many", ITEM, stdin=json.dumps(batch))
    return write


def _cli_spec(flag):
    def write(monkeypatch, db, text):
        args = {"--title": "a spec", "--body": "a body"}
        args[flag] = text
        return refused(monkeypatch, db, "spec", "set", ITEM,
                       *itertools.chain.from_iterable(args.items()))
    return write


def _lib_append(kind, field):
    def write(db, text):
        with open_ledger(db, create=False) as lg:
            add(lg, kind, {field: text})
    return write


def _lib_spec(field):
    def write(db, text):
        content = {"title": "a spec", "body_md": "a body", field: text}
        with open_ledger(db, create=False) as lg:
            set_spec(lg, ITEM, content, harness="pytest")
    return write


MULTI_CLI = [
    pytest.param("item.note.text", _cli_note, id="note-verb"),
    pytest.param("item.note.text", _cli_append("item.note", "text"), id="note-append"),
    pytest.param("item.note.text", _cli_append_many("item.note", "text"), id="note-append-many"),
    pytest.param("item.progress.text", _cli_append("item.progress", "text"), id="progress-append"),
    pytest.param("item.progress.text", _cli_append_many("item.progress", "text"),
                 id="progress-append-many"),
    pytest.param("item.spec.body_md", _cli_spec("--body"), id="spec-body"),
    pytest.param("item.spec.acceptance", _cli_spec("--acceptance"), id="spec-acceptance"),
]
MULTI_LIB = [
    pytest.param("item.note.text", _lib_append("item.note", "text"), id="note"),
    pytest.param("item.progress.text", _lib_append("item.progress", "text"), id="progress"),
    pytest.param("item.spec.body_md", _lib_spec("body_md"), id="spec-body"),
    pytest.param("item.spec.acceptance", _lib_spec("acceptance"), id="spec-acceptance"),
]


@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "ledger.db"
    with open_ledger(path) as lg:
        item(lg)
    return path


def _message_is_the_reason(message: str, reason: str, char: str) -> None:
    if char == "\r":
        assert message == f"{reason}: U+000D: convert line endings to LF"
    else:
        assert message == reason
    assert char not in message


@pytest.mark.parametrize("char", MULTI_REFUSED)
@pytest.mark.parametrize("field,write", MULTI_CLI)
def test_multi_line_item_text_with_a_control_is_refused_from_the_cli(
        db, monkeypatch, field, write, char) -> None:
    before = event_count(db)
    message = write(monkeypatch, db, "a" + char + "b")
    _message_is_the_reason(message, f"display_control_character:{field}", char)
    assert event_count(db) == before


@pytest.mark.parametrize("char", MULTI_REFUSED)
@pytest.mark.parametrize("field,write", MULTI_LIB)
def test_multi_line_item_text_with_a_control_is_refused_from_the_library(
        db, field, write, char) -> None:
    before = event_count(db)
    with pytest.raises(ValidationError) as caught:
        write(db, "a" + char + "b")
    assert caught.value.reason == f"display_control_character:{field}"
    assert char not in str(caught.value)
    assert event_count(db) == before


def _cli_new(monkeypatch, db, text):
    return refused(monkeypatch, db, "new", "--type", "ticket", "--id", "esc-2", "--title", text)


def _cli_append_created(monkeypatch, db, text):
    payload = {"type": "ticket", "title": text}
    return refused(monkeypatch, db, "append", "--kind", "item.created", "--item", "esc-2",
                   "--payload", json.dumps(payload))


def _lib_created(db, text):
    with open_ledger(db, create=False) as lg:
        add(lg, "item.created", {"type": "ticket", "title": text}, "esc-2")


ONE_CLI = [
    pytest.param("item.created.title", _cli_new, id="new"),
    pytest.param("item.created.title", _cli_append_created, id="append"),
    pytest.param("item.spec.title", _cli_spec("--title"), id="spec-title"),
]
ONE_LIB = [
    pytest.param("item.created.title", _lib_created, id="created"),
    pytest.param("item.spec.title", _lib_spec("title"), id="spec-title"),
]


@pytest.mark.parametrize("char", ONE_LINE_REFUSED)
@pytest.mark.parametrize("field,write", ONE_CLI)
def test_a_title_with_a_control_tab_or_newline_is_refused_from_the_cli(
        db, monkeypatch, field, write, char) -> None:
    before = event_count(db)
    message = write(monkeypatch, db, "a" + char + "b")
    _message_is_the_reason(message, f"display_control_character:{field}", char)
    assert event_count(db) == before


@pytest.mark.parametrize("char", ONE_LINE_REFUSED)
@pytest.mark.parametrize("field,write", ONE_LIB)
def test_a_title_with_a_control_tab_or_newline_is_refused_from_the_library(
        db, field, write, char) -> None:
    before = event_count(db)
    with pytest.raises(ValidationError) as caught:
        write(db, "a" + char + "b")
    assert caught.value.reason == f"display_control_character:{field}"
    assert event_count(db) == before


MARKDOWN = (
    " # Heading\n\n\tcode\n- item  \n"
    + "e\u0301 " + "\U0001f468\u200d\U0001f469 " + "\u200e " + "\u2764\ufe0f\n"
)
TITLE = "Fix \u00e9 \u2764\ufe0f now"


def test_ordinary_markdown_and_titles_still_write(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    text_out(monkeypatch, db, "new", "--type", "ticket", "--id", ITEM, "--title", TITLE)
    text_out(monkeypatch, db, "note", ITEM, "--text", MARKDOWN)
    text_out(monkeypatch, db, "append", "--kind", "item.progress", "--item", ITEM,
             "--payload", json.dumps({"text": MARKDOWN}))
    code, out, err = run_cli(
        monkeypatch, ["--db", str(db), "append-many", ITEM],
        stdin=json.dumps([{"kind": "item.note", "payload": {"text": MARKDOWN}}]),
    )
    assert code == 0, err
    text_out(monkeypatch, db, "spec", "set", ITEM, "--title", TITLE, "--body", MARKDOWN,
             "--acceptance", MARKDOWN)
    with open_ledger(db, create=False) as lg:
        add(lg, "item.created", {"type": "ticket", "title": TITLE}, "esc-2")
        add(lg, "item.note", {"text": MARKDOWN}, "esc-2")
        set_spec(lg, "esc-2", {"title": TITLE, "body_md": MARKDOWN}, harness="pytest")
    out = text_out(monkeypatch, db, "spec", "show", ITEM)
    assert MARKDOWN.rstrip("\n") in out
    assert "\tcode\n" in out


# --- T9: rows written before the rule still read ------------------------------------

LEGACY = {
    "title": "T" + ESC + "[31m",
    "note": "n" + OSC,
    "progress": "p" + ESC + "[2J",
    "body_md": "b" + ESC + "[2J\n",
    "acceptance": "a" + ESC + "[2J",
}


def _legacy(db) -> int:
    with pre_rule(), open_ledger(db) as lg:
        item(lg, title=LEGACY["title"])
        note = add(lg, "item.note", {"text": LEGACY["note"]}, key="legacy-note")
        add(lg, "item.progress", {"text": LEGACY["progress"]})
        spec(lg, body_md=LEGACY["body_md"], acceptance=LEGACY["acceptance"])
    return note.id


def _payloads(db) -> list[tuple[str, str]]:
    with open_ledger(db, create=False) as lg:
        return [(event.kind, canonical_dumps(event.payload)) for event in lg.events()]


def _reads_escaped(monkeypatch, db) -> None:
    card = text_out(monkeypatch, db, "card", ITEM)
    assert_no_raw(card, ESC, BEL)
    assert r"n\u001b]52;c;aGk=\u0007" in card
    shown = text_out(monkeypatch, db, "spec", "show", ITEM)
    assert_no_raw(shown, ESC)
    assert r"b\u001b[2J" in shown


def test_rows_written_before_the_rule_verify_export_restore_and_replay(
        tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    note_id = _legacy(db)
    original = _payloads(db)
    with open_ledger(db, create=False) as lg:
        lg.verify()
    code, out, err = run_cli(monkeypatch, ["--db", str(db), "export-item", ITEM])
    assert code == 0, err
    export_dir = tmp_path / "export"
    with open_ledger(db, create=False) as lg:
        export_events(lg, export_dir)
    events = verify_export(export_dir)
    restored = tmp_path / "restored.db"
    restore_export(export_dir, restored, system="home")
    with open_ledger(restored, create=False) as lg:
        lg.verify()
    restored_payloads = [row for row in _payloads(restored) if row[0] != "ledger.system"]
    assert restored_payloads == original
    _reads_escaped(monkeypatch, restored)
    replayed = tmp_path / "replayed.db"
    with open_ledger(replayed) as lg:
        replay_events(lg, events)
        lg.verify()
    assert _payloads(replayed) == original
    _reads_escaped(monkeypatch, replayed)
    # An exact keyed retry of the stored note returns it; a new key is refused.
    with open_ledger(db, create=False) as lg:
        again = add(lg, "item.note", {"text": LEGACY["note"]}, key="legacy-note")
        assert again.id == note_id
        with pytest.raises(ValidationError) as caught:
            add(lg, "item.note", {"text": LEGACY["note"]}, key="legacy-note-2")
    assert caught.value.reason == "display_control_character:item.note.text"


# --- T10: an item id holding a control -----------------------------------------------


def test_an_item_id_with_a_control_prints_escaped_in_header_and_commands(
        tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    item_id = "X" + ESC + "[2J"
    with pre_rule(), open_ledger(db) as lg:
        item(lg, item_id=item_id)
    out = text_out(monkeypatch, db, "card", "--", item_id)
    assert_no_raw(out, ESC)
    shown = lines(out.rstrip("\n"))
    assert shown[0] == "# " + r"X\u001b[2J"
    assert "$'X\\x1b[2J'" in shown[-1]


# An id holding every kind of code point a printed command must carry safely.
HOSTILE_ID = "X" + ESC + "[2J'q\\" + RLO + LS + "\n" + BEL + "\t y"


def _bash_args(command: str) -> list[str]:
    """The arguments bash passes to ``ledger`` when it runs ``command``."""
    script = "ledger() { printf '%s\\0' \"$@\"; }\n" + command
    out = subprocess.run(["bash", "-c", script], capture_output=True, check=True).stdout
    return out.decode("utf-8").split("\0")[:-1]


@pytest.mark.parametrize("build,expected", [
    pytest.param(recover_command, ["export-item", "--json", "--", HOSTILE_ID], id="recover"),
    pytest.param(lambda item_id, program: footer_line(item_id, program)[len(FOOTER) + 2:-1],
                 ["export-item", "--", HOSTILE_ID], id="footer"),
    pytest.param(children_command, ["query", f"--parent={HOSTILE_ID}", "--all"], id="children"),
    pytest.param(duplicates_command, ["query", f"--duplicate-of={HOSTILE_ID}", "--all"],
                 id="duplicates"),
    pytest.param(blockers_command, ["query", f"--blocks={HOSTILE_ID}", "--open", "--all"],
                 id="blockers"),
    pytest.param(
        lambda item_id, program: derive_next_step(
            status="open", blockers=(), open_questions=(), owner=None, claim=None,
            item_id=item_id, program=program,
        )[len("Resume with: "):],
        ["resume", f"--item={HOSTILE_ID}"], id="next-step"),
])
def test_a_printed_command_round_trips_a_control_bearing_id(build, expected) -> None:
    command = build(HOSTILE_ID, "ledger")
    assert_no_raw(command, ESC, BEL, RLO, LS, "\n", "\t")
    assert command.startswith("ledger ")
    assert _bash_args(command) == expected


def test_the_card_json_commands_match_the_printed_ones(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg, item_id=HOSTILE_ID)
    out = text_out(monkeypatch, db, "card", "--", HOSTILE_ID)
    raw, data = json_out(monkeypatch, db, "card", "--", HOSTILE_ID)
    assert_no_raw(raw, ESC)
    assert data["footer"] in lines(out)
    assert_no_raw(data["footer"], ESC, BEL, RLO, LS, "\n", "\t")
    assert _bash_args(data["footer"][len(FOOTER) + 2:-1])[-1] == HOSTILE_ID
    assert_no_raw(data["next_step"], ESC, BEL, RLO, LS, "\n", "\t")
    step = data["next_step"]
    assert step.startswith("Resume with: ")
    assert _bash_args(step[len("Resume with: "):])[-2:] == ["resume", f"--item={HOSTILE_ID}"]


# --- T11, T11b: spec diff -------------------------------------------------------------


def test_spec_diff_shows_a_stored_control_changed_to_its_escape_text(
        tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg)
        spec(lg, body_md="a" + ESC + "\n")
        spec(lg, body_md="a" + r"\u001b" + "\n")
    out = text_out(monkeypatch, db, "spec", "diff", ITEM, "1", "2")
    assert_no_raw(out, ESC)
    shown = lines(out)
    assert "no content change" not in shown
    body = shown.index("## body_md")
    assert r"-a\u001b" in shown[body:]
    assert r"+a\u001b" in shown[body:]


DIFF_ID = "D" + OSC


def _diff_item(db) -> None:
    with pre_rule(), open_ledger(db) as lg:
        item(lg, item_id=DIFF_ID)
        spec(lg, DIFF_ID, title="A", body_md="p\nq\n")
        spec(lg, DIFF_ID, title="A\tB", body_md="p" + ESC + "[2J" + RLO + "\nq" + LS + "r\n")


def test_spec_diff_escapes_each_printed_line(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    _diff_item(db)
    out = text_out(monkeypatch, db, "spec", "diff", "--", DIFF_ID, "1", "2")
    assert_no_raw(out, ESC, BEL, RLO, LS, "\t")
    shown = lines(out)
    assert shown[0].startswith(r"spec diff D\u001b]52;c;aGk=\u0007 v1")
    assert r"+A\tB" in shown
    assert r"+p\u001b[2J\u202e" in shown
    assert r"+q\u2028r" in shown


def test_spec_diff_json_keeps_the_stored_code_points(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    _diff_item(db)
    raw, data = json_out(monkeypatch, db, "spec", "diff", "--", DIFF_ID, "1", "2")
    assert_no_raw(raw, ESC, RLO, LS)
    fields = {entry["field"]: entry for entry in data["fields"]}
    assert fields["title"]["from"] == "A" and fields["title"]["to"] == "A\tB"
    assert fields["body_md"]["to"] == "p" + ESC + "[2J" + RLO + "\nq" + LS + "r\n"
    assert "+A\tB" in fields["title"]["diff"]
    assert any(ESC + "[2J" + RLO in line for line in fields["body_md"]["diff"])
    # Split on LF only: the line separator stays inside its diff line.
    assert "+q" + LS + "r" in fields["body_md"]["diff"]


def test_spec_diff_keeps_an_absent_field_absent(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg)
        spec(lg, body_md="b\n")
        spec(lg, body_md="b\n", acceptance="ok" + ESC + "[2J")
        spec(lg, body_md="b\n")
    added = text_out(monkeypatch, db, "spec", "diff", ITEM, "1", "2")
    removed = text_out(monkeypatch, db, "spec", "diff", ITEM, "2", "3")
    assert r"+ok\u001b[2J" in lines(added)
    assert r"-ok\u001b[2J" in lines(removed)
    for out in (added, removed):
        assert_no_raw(out, ESC)
        assert not any("None" in line for line in lines(out))
    _raw, data = json_out(monkeypatch, db, "spec", "diff", ITEM, "1", "2")
    assert {e["field"]: e for e in data["fields"]}["acceptance"]["from"] is None
    _raw, data = json_out(monkeypatch, db, "spec", "diff", ITEM, "2", "3")
    assert {e["field"]: e for e in data["fields"]}["acceptance"]["to"] is None


# --- T12: --json keeps stored text --------------------------------------------------


def test_json_forms_keep_stored_text_and_print_no_raw_control(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    stored = "n" + ESC + "[2J" + CSI_C1 + "x"
    with pre_rule(), open_ledger(db) as lg:
        item(lg)
        add(lg, "item.note", {"text": stored})
        spec(lg, body_md=stored)
    raw, data = json_out(monkeypatch, db, "card", ITEM)
    assert_no_raw(raw, ESC, CSI_C1)
    assert data["notes"][0]["text"] == stored
    raw, data = json_out(monkeypatch, db, "spec", "show", ITEM)
    assert_no_raw(raw, ESC, CSI_C1)
    assert data["body_md"] == stored


# --- T13, T19: export-item ------------------------------------------------------------


def test_export_item_ticket_spec_text_is_escaped(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    body = "first" + ESC + "[2J\nsecond" + RLO + "\n"
    with pre_rule(), open_ledger(db) as lg:
        item(lg)
        spec(lg, title="t" + ESC + RLO, body_md=body, acceptance="a" + ESC + RLO)
    out = text_out(monkeypatch, db, "export-item", "--ticket", ITEM)
    assert_no_raw(out, ESC, RLO)
    assert r"\u001b" in out and r"\u202e" in out
    shown = lines(out)
    assert r"first\u001b[2J" in shown and r"second\u202e" in shown
    assert "title: " + r"t\u001b\u202e" in shown
    raw, data = json_out(monkeypatch, db, "export-item", "--ticket", ITEM)
    assert_no_raw(raw, ESC)
    assert data["spec"]["body_md"] == body
    assert ESC in data["ticket"]
    plain = text_out(monkeypatch, db, "export-item", ITEM)
    assert_no_raw(plain, ESC, RLO)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, text=True, capture_output=True,
    ).stdout


def test_export_item_ticket_file_text_is_escaped(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    section = "### Ticket: SL1 \u2014 A ticket\n\tstep" + ESC + "[2J" + RLO + "\nnext" + LS + "line\n"
    (root / "a.md").write_bytes(("# Roadmap\n\n" + section).encode("utf-8"))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    sha = _git(root, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(root)
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        add(lg, "item.created", {"type": "ticket", "title": "A ticket", "source": f"a.md:3@{sha}"})
    out = text_out(monkeypatch, db, "export-item", "--ticket", ITEM)
    assert_no_raw(out, ESC, RLO, LS)
    shown = lines(out)
    assert "\tstep" + r"\u001b[2J\u202e" in shown
    assert r"next\u2028line" in shown
    raw, data = json_out(monkeypatch, db, "export-item", "--ticket", ITEM)
    assert_no_raw(raw, ESC)
    assert data["ticket"] == section


# --- T14: context ---------------------------------------------------------------------

CTX = ESC + "[2J" + BEL + RLO


def test_context_escapes_every_control_left_after_flattening(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg, title="t" + CTX + "\tz")
        add(lg, "item.note", {"text": "n" + CTX + "\tz"})
        spec(lg, body_md="s" + CTX + "\tz\nx" + LS + "y" + PS + "z" + NEL + "w\n")
    out = text_out(monkeypatch, db, "context", ITEM)
    assert_no_raw(out, ESC, BEL, RLO, LS, PS, NEL)
    assert r"\u001b" in out and r"\u0007" in out and r"\u202e" in out
    shown = lines(out)
    assert any(line.endswith(r"x\u2028y\u2029z\u0085w") for line in shown), out
    assert not any(line.startswith(("y", "z", "w")) for line in shown)
    raw, data = json_out(monkeypatch, db, "context", ITEM)
    assert_no_raw(raw, ESC)
    notes = {s["name"]: s for s in data["sections"]}["notes"]
    assert any(ESC in line for line in notes["lines"])


# --- T15: item listings ---------------------------------------------------------------

LIST_TITLE = "T" + ESC + "[2J\tX" + RLO


@pytest.mark.parametrize("verb", ["query", "ready"])
def test_item_listings_escape_the_title_and_keep_four_columns(
        tmp_path, monkeypatch, verb) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg, title=LIST_TITLE)
    out = text_out(monkeypatch, db, verb)
    assert_no_raw(out, ESC, RLO)
    [row] = [line for line in lines(out) if line.startswith(ITEM + "\t")]
    columns = row.split("\t")
    assert len(columns) == 4
    assert columns[3] == r"T\u001b[2J\tX\u202e"
    _raw, data = json_out(monkeypatch, db, verb)
    [found] = [entry for entry in data["items"] if entry["item_id"] == ITEM]
    assert found["title"] == LIST_TITLE


# --- T16: bidirectional controls on the card -------------------------------------------


def test_card_escapes_bidi_controls_in_notes_and_child_titles(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    with pre_rule(), open_ledger(db) as lg:
        item(lg)
        add(lg, "item.note", {"text": "a" + RLO + "b"})
        item(lg, item_id="esc-child", title="c" + LRI + "d")
        add(lg, "item.linked", {"rel": "child-of", "target": ITEM}, "esc-child")
    out = text_out(monkeypatch, db, "card", ITEM)
    assert_no_raw(out, *BIDI)
    assert r"a\u202eb" in out
    assert r"c\u2066d" in out


# --- T17: a body file with CRLF line endings ---------------------------------------------


def test_a_crlf_body_file_is_refused_with_line_ending_advice(db, tmp_path, monkeypatch) -> None:
    reason = "display_control_character:item.spec.body_md"
    body_file = tmp_path / "body.md"
    body_file.write_bytes(b"line\r\nnext\r\n")
    before = event_count(db)
    message = refused(monkeypatch, db, "spec", "set", ITEM, "--title", "t",
                      "--body-file", str(body_file))
    assert message.startswith(reason)
    assert message.endswith("convert line endings to LF")
    assert event_count(db) == before
    with pytest.raises(ValidationError) as caught:
        _lib_spec("body_md")(db, body_file.read_bytes().decode("utf-8"))
    assert caught.value.reason == reason
    body_file.write_bytes(b"line" + ESC.encode() + b"[2J\n")
    message = refused(monkeypatch, db, "spec", "set", ITEM, "--title", "t",
                      "--body-file", str(body_file))
    assert message == reason
    assert event_count(db) == before


# --- T18: search ---------------------------------------------------------------------------

SEARCH = ESC + "[2J" + RLO


def test_search_escapes_titles_and_snippets(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    title = "zebra" + SEARCH + "title"
    with pre_rule(), open_ledger(db) as lg:
        item(lg, title=title)
        add(lg, "item.note", {"text": "zebra" + SEARCH + "note"})
    out = text_out(monkeypatch, db, "search", "zebra")
    assert_no_raw(out, ESC, RLO)
    shown = lines(out)
    [row] = [line for line in shown if "\t" + ITEM + "\t" in line]
    assert row.split("\t")[3] == r"zebra\u001b[2J\u202etitle"
    snippet = shown[shown.index(row) + 1]
    assert snippet.startswith("\t") and r"\u001b[2J\u202e" in snippet
    _raw, data = json_out(monkeypatch, db, "search", "zebra")
    assert data["cards"][0]["title"] == title
