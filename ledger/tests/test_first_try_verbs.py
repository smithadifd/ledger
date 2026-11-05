"""First-try verb shapes, the export-item completeness line, recoverable
card cuts, query date filters and the search-index refresh.

Every ledger here is a temp DB built through the CLI or the library.
"""
from __future__ import annotations

import io
import json
import re
import shlex
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger.card import format_card, project_card
from ledger.cli import main
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, EXIT_REFUSED, EXIT_USAGE

ITEM = "it-1a2b3c4d5e6f"


def invoke(monkeypatch, argv):
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "state" / "ledger.db")


def _seed(monkeypatch, db: str, item_id: str = ITEM) -> None:
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", item_id, "--type", "ticket", "--title", "first try"],
    )
    assert code == 0, err


def _events(db: str, item_id: str = ITEM):
    with open_ledger(db, create=False) as lg:
        return [(e.kind, e.payload) for e in lg.events(item_id=item_id)]


# --- link ------------------------------------------------------------------


def test_link_accepts_a_local_file_path_with_rel_and_target(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)
    brief = tmp_path / "brief.md"
    brief.write_text("# brief\n", encoding="utf-8")

    code, _out, err = invoke(
        monkeypatch, ["--db", db, "link", ITEM, "--rel", "url", "--target", str(brief)]
    )

    assert code == 0, err
    assert _events(db)[-1] == ("item.linked", {"rel": "url", "target": str(brief)})


def test_link_positional_target_defaults_rel_to_url(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)

    code, out, err = invoke(monkeypatch, ["--db", db, "link", ITEM, "/abs/brief.md"])

    assert code == 0, err
    assert out.startswith(f"linked\t{ITEM}\turl\t/abs/brief.md\t")
    assert _events(db)[-1] == ("item.linked", {"rel": "url", "target": "/abs/brief.md"})


def test_link_positional_target_takes_an_explicit_rel(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)

    code, _out, err = invoke(monkeypatch, ["--db", db, "link", ITEM, "--rel", "pr", "123"])

    assert code == 0, err
    assert _events(db)[-1] == ("item.linked", {"rel": "pr", "target": "123"})


@pytest.mark.parametrize(
    "tail",
    [
        ["a.md", "--target", "b.md"],  # two targets
        ["url", "/tmp/x.md"],  # a rel word then a target: no such form
        ["--rel", "pr"],  # no target at all
        [],
    ],
)
def test_link_with_no_matching_form_is_a_usage_error_naming_the_shape(
    tmp_path, monkeypatch, tail
):
    db = _db(tmp_path)
    _seed(monkeypatch, db)
    before = _events(db)

    code, out, err = invoke(monkeypatch, ["--db", db, "link", ITEM, *tail])

    assert code == EXIT_USAGE
    assert out == ""
    assert "link ITEM TARGET" in err
    assert "--target" in err
    assert _events(db) == before


# --- note ------------------------------------------------------------------


def test_note_positional_text(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)

    code, _out, err = invoke(monkeypatch, ["--db", db, "note", ITEM, "free text"])

    assert code == 0, err
    assert _events(db)[-1] == ("item.note", {"text": "free text"})


def test_note_text_flag_still_works(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)

    code, _out, err = invoke(monkeypatch, ["--db", db, "note", ITEM, "--text", "flagged"])

    assert code == 0, err
    assert _events(db)[-1] == ("item.note", {"text": "flagged"})


@pytest.mark.parametrize("tail", [["one", "--text", "two"], []])
def test_note_needs_exactly_one_text(tmp_path, monkeypatch, tail):
    db = _db(tmp_path)
    _seed(monkeypatch, db)
    before = _events(db)

    code, out, err = invoke(monkeypatch, ["--db", db, "note", ITEM, *tail])

    assert code == EXIT_USAGE
    assert out == ""
    assert "note ITEM TEXT" in err
    assert _events(db) == before


# --- help examples ---------------------------------------------------------


def _help(monkeypatch, verb: str) -> str:
    code, out, _err = invoke(monkeypatch, [verb, "--help"])
    assert code == EXIT_OK
    return out


def _examples(text: str) -> list[list[str]]:
    return [
        shlex.split(line)[1:]
        for line in text.splitlines()
        if line.startswith("  ledger ")
    ]


@pytest.mark.parametrize("verb", ["note", "link", "elect"])
def test_help_example_runs_green_on_a_fresh_database(tmp_path, monkeypatch, verb):
    text = _help(monkeypatch, verb)
    examples = _examples(text)
    assert examples, f"{verb} --help has no worked example"
    for argv in examples:
        assert argv[0] == verb
        db = _db(tmp_path / argv[1] / str(len(argv)))
        _seed(monkeypatch, db, argv[1])
        if verb == "elect":
            quote = argv[argv.index("--quote") + 1]
            quote_ref = int(argv[argv.index("--quote-ref") + 1])
            code, out, err = invoke(
                monkeypatch,
                [
                    "--json", "--db", db, "append", "--kind", "utterance.captured",
                    "--payload", json.dumps({"text": quote}),
                    "--channel", "terminal", "--provenance-class", "submitted-prompt",
                ],
            )
            assert code == 0, err
            assert json.loads(out)["data"]["event"]["id"] == quote_ref
        code, _out, err = invoke(monkeypatch, ["--db", db, *argv])
        assert code == 0, f"{argv}: {err}"


def test_elect_help_says_rejected_is_required_and_repeatable(monkeypatch):
    text = " ".join(_help(monkeypatch, "elect").split())
    # The option's own entry, not the closing paragraph.
    entry = re.search(r"--rejected REJECTED ([^-].*?) --scope SCOPE", text)
    assert entry is not None
    assert "required" in entry.group(1)
    assert "repeatable" in entry.group(1)


def test_elect_still_refuses_a_missing_rejected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)
    code, _out, err = invoke(
        monkeypatch,
        [
            "--db", db, "elect", ITEM, "--question", "q", "--chosen", "a",
            "--chosen-description", "d", "--scope", "s", "--quote-ref", "1",
            "--quote", "x",
        ],
    )
    assert code == EXIT_USAGE
    assert "--rejected" in err


# --- search words ----------------------------------------------------------


def test_search_takes_several_unquoted_words(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)
    invoke(monkeypatch, ["--db", db, "note", ITEM, "--text", "zebra stripes decision"])

    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "search", "zebra", "stripes"])

    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["query"] == "zebra stripes"
    assert [card["id"] for card in data["cards"]] == [ITEM]


def test_search_still_takes_one_quoted_phrase(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)
    invoke(monkeypatch, ["--db", db, "note", ITEM, "--text", "zebra stripes decision"])

    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "search", "zebra stripes"])

    assert code == 0, err
    assert json.loads(out)["data"]["query"] == "zebra stripes"


# --- search refresh --------------------------------------------------------


def _doctor_search(monkeypatch, db: str) -> str:
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "doctor"])
    assert code == 0, err
    return json.loads(out)["data"]["search"]["status"]


def test_stale_index_search_returns_the_document_and_doctor_reads_current(
    tmp_path, monkeypatch
):
    db = _db(tmp_path)
    _seed(monkeypatch, db)
    code, _out, err = invoke(monkeypatch, ["--db", db, "index"])
    assert code == 0, err
    invoke(monkeypatch, ["--db", db, "note", ITEM, "--text", "quokka findings"])
    assert _doctor_search(monkeypatch, db) == "stale"

    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "search", "quokka"])

    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["refreshed"] is True
    assert [card["id"] for card in data["cards"]] == [ITEM]
    assert _doctor_search(monkeypatch, db) == "current"


def test_search_that_cannot_rewrite_a_stale_index_refuses_by_name(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)
    code, _out, err = invoke(monkeypatch, ["--db", db, "index"])
    assert code == 0, err
    invoke(monkeypatch, ["--db", db, "note", ITEM, "--text", "quokka findings"])
    assert _doctor_search(monkeypatch, db) == "stale"

    def refuse_write(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(search_mod.tempfile, "mkstemp", refuse_write)
    code, out, err = invoke(monkeypatch, ["--db", db, "search", "quokka"])

    assert code == EXIT_REFUSED
    assert out == ""
    assert "cannot rebuild the stale search index" in err
    assert "search.db" in err
    assert "Traceback" not in err
    assert _doctor_search(monkeypatch, db) == "stale"


# --- export-item completeness ----------------------------------------------


def _item_with_a_decayed_event(db: str) -> None:
    with open_ledger(db) as lg:
        lg.append(
            kind="item.created", item_id=ITEM, idempotency_key="k1", harness="pytest",
            payload={"type": "ticket", "title": "decay"},
        )
        lg.append(
            kind="item.progress", item_id=ITEM, idempotency_key="k2", harness="pytest",
            payload={"text": "bookkeeping"},
        )
        lg.append(
            kind="item.state", item_id=ITEM, idempotency_key="k3", harness="pytest",
            payload={"status": "closed"},
        )


def test_export_item_text_ends_with_the_completeness_line(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _item_with_a_decayed_event(db)

    code, out, err = invoke(monkeypatch, ["--db", db, "export-item", ITEM])

    assert code == 0, err
    lines = out.rstrip("\n").split("\n")
    assert len([line for line in lines if line.startswith("completeness")]) == 1
    assert lines[-1] == (
        "completeness: 2 events returned, 3 events on the item, 1 decayed"
    )
    assert [json.loads(line)["kind"] for line in lines[:-1]] == ["item.created", "item.state"]


def test_export_item_json_carries_the_completeness_object(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _item_with_a_decayed_event(db)

    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "export-item", ITEM])

    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["completeness"] == {
        "events_returned": 2,
        "events_on_item": 3,
        "events_decayed": 1,
    }
    assert len(data["events"]) == 2


def test_export_item_on_an_open_item_decays_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _seed(monkeypatch, db)

    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "export-item", ITEM])

    assert code == 0, err
    assert json.loads(out)["data"]["completeness"] == {
        "events_returned": 1,
        "events_on_item": 1,
        "events_decayed": 0,
    }


# --- card cut markers ------------------------------------------------------


def test_cut_notes_marker_names_the_recovery_command(tmp_path):
    with open_ledger(_db(tmp_path)) as lg:
        lg.append(
            kind="item.created", item_id=ITEM, idempotency_key="c", harness="pytest",
            payload={"type": "ticket", "title": "many notes"},
        )
        for index in range(60):
            lg.append(
                kind="item.note", item_id=ITEM, idempotency_key=f"n{index}",
                harness="pytest",
                payload={"text": " ".join(f"word{index}x{i}" for i in range(12))},
            )
        text = format_card(project_card(lg, ITEM))

    assert re.search(
        rf"## Notes\n\(\+\d+ cut; ledger export-item --json -- {ITEM}\)\n", text
    )
    assert "(+" in text and not re.search(r"\(\+\d+ cut\)", text)


def test_card_footer_names_what_is_authoritative(tmp_path):
    with open_ledger(_db(tmp_path)) as lg:
        lg.append(
            kind="item.created", item_id=ITEM, idempotency_key="c", harness="pytest",
            payload={"type": "ticket", "title": "footer"},
        )
        text = format_card(project_card(lg, ITEM))

    assert text.rstrip("\n").endswith(
        f"This card is a projection; the events are the authority (ledger export-item -- {ITEM})"
    )


# --- query --since / --until -----------------------------------------------


@pytest.fixture
def dated_db(tmp_path):
    path = _db(tmp_path)
    with open_ledger(path) as lg:
        for item_id, title, at in (
            ("it-old", "old item", "2026-01-10T09:00:00.000Z"),
            ("it-new", "new item", "2026-03-10T09:00:00.000Z"),
        ):
            lg.append(
                kind="item.created", item_id=item_id, idempotency_key=f"k-{item_id}",
                harness="fixture", recorded_at=at,
                payload={"type": "ticket", "title": title},
            )
    return path


def _query_ids(monkeypatch, db, *flags):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query", *flags])
    assert code == 0, err
    return [item["item_id"] for item in json.loads(out)["data"]["items"]]


def test_query_since_and_until_filter_on_the_newest_event(dated_db, monkeypatch):
    assert _query_ids(monkeypatch, dated_db, "--since", "2026-02-01") == ["it-new"]
    assert _query_ids(monkeypatch, dated_db, "--until", "2026-02-01") == ["it-old"]
    assert _query_ids(
        monkeypatch, dated_db, "--since", "2026-01-10", "--until", "2026-01-10"
    ) == ["it-old"]
    assert _query_ids(
        monkeypatch, dated_db, "--since", "2026-01-10T09:00:00Z", "--until", "2026-03-10"
    ) == ["it-new", "it-old"]


def test_query_dates_an_item_by_its_newest_event(dated_db, monkeypatch):
    with open_ledger(dated_db) as lg:
        lg.append(
            kind="item.note", item_id="it-old", idempotency_key="late-note",
            harness="fixture", recorded_at="2026-03-20T09:00:00.000Z",
            payload={"text": "revived"},
        )

    assert _query_ids(monkeypatch, dated_db, "--since", "2026-03-15") == ["it-old"]
    code, _out, _err = invoke(
        monkeypatch, ["--db", dated_db, "query", "--until", "2026-02-01"]
    )
    assert code == EXIT_NOT_FOUND


def test_query_since_with_nothing_in_range_is_empty(dated_db, monkeypatch):
    code, _out, err = invoke(
        monkeypatch, ["--db", dated_db, "query", "--since", "2027-01-01"]
    )
    assert code == EXIT_NOT_FOUND
    assert "no matching rows" in err


@pytest.mark.parametrize(
    "bad",
    ["20261031", "2026-13-45", "yesterday", "2026-10-31T09", "2026-10-31 09:30", "0000-00-00"],
)
def test_query_since_refuses_the_forms_search_since_refuses(dated_db, monkeypatch, bad):
    s_code, s_out, s_err = invoke(
        monkeypatch, ["--db", dated_db, "search", "item", "--since", bad]
    )
    q_code, q_out, q_err = invoke(
        monkeypatch, ["--db", dated_db, "query", "--since", bad]
    )

    assert s_code == EXIT_USAGE
    assert (q_code, q_out, q_err) == (s_code, s_out, s_err)
    u_code, _u_out, u_err = invoke(
        monkeypatch, ["--db", dated_db, "query", "--until", bad]
    )
    assert u_code == EXIT_USAGE
    assert "--until" in u_err


def test_query_since_after_until_is_a_usage_error(dated_db, monkeypatch):
    code, _out, err = invoke(
        monkeypatch,
        ["--db", dated_db, "query", "--since", "2026-03-01", "--until", "2026-02-01"],
    )
    assert code == EXIT_USAGE
    assert "--since is after --until" in err


def test_query_without_dates_is_unchanged(dated_db, monkeypatch):
    assert _query_ids(monkeypatch, dated_db) == ["it-new", "it-old"]


# --- printed commands stay one safe token per id ---------------------------

HOSTILE_IDS = ["x; touch PWN", "-lead", "a b$(touch PWN)"]


@pytest.mark.parametrize("item_id", HOSTILE_IDS)
def test_printed_commands_keep_a_hostile_id_one_token(tmp_path, monkeypatch, item_id):
    import shlex
    import subprocess

    db = _db(tmp_path)
    with open_ledger(db) as lg:
        lg.append(
            kind="item.created", item_id=item_id, idempotency_key="c", harness="pytest",
            payload={"type": "ticket", "title": "hostile"},
        )
        for index in range(60):
            lg.append(
                kind="item.note", item_id=item_id, idempotency_key=f"n{index}",
                harness="pytest",
                payload={"text": " ".join(f"word{index}x{i}" for i in range(12))},
            )
        text = format_card(project_card(lg, item_id))

    marker = re.search(r"## Notes\n\(\+\d+ cut; (ledger [^\n]*)\)\n", text)
    footer = re.search(r"\((ledger export-item [^\n]*)\)\n\Z", text)
    assert marker and footer
    expected = {
        marker.group(1): ["ledger", "export-item", "--json", "--", item_id],
        footer.group(1): ["ledger", "export-item", "--", item_id],
    }
    for command, tokens in expected.items():
        # One token for the id, however many metacharacters it holds.
        assert shlex.split(command) == tokens
        # A shell reads it as data: swap the program for printf and nothing runs.
        proc = subprocess.run(
            ["sh", "-c", command.replace("ledger", "printf '%s\\n'", 1)],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert proc.stdout.splitlines() == tokens[1:]
        assert not (tmp_path / "PWN").exists()
        # And the ledger itself runs it: the id, hyphen first or not, is found.
        code, out, err = invoke(monkeypatch, ["--db", db, *tokens[1:]])
        assert code == 0, err
        assert item_id in out


@pytest.mark.parametrize("item_id", HOSTILE_IDS)
def test_next_step_resume_command_keeps_a_hostile_id_one_token(tmp_path, monkeypatch, item_id):
    import shlex

    db = _db(tmp_path)
    with open_ledger(db) as lg:
        lg.append(
            kind="item.created", item_id=item_id, idempotency_key="c", harness="pytest",
            payload={"type": "ticket", "title": "hostile"},
        )
        step = project_card(lg, item_id).next_step

    assert step.startswith("Resume with: ")
    tokens = shlex.split(step[len("Resume with: "):])
    assert tokens == ["ledger", "resume", f"--item={item_id}"]
    code, out, err = invoke(monkeypatch, ["--db", db, *tokens[1:]])
    assert code == 0, err
    assert item_id in out
