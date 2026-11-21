"""``ledger search`` text output caps titles and says when it was cut.

JSON is unchanged: each card keeps its stored title, and ``truncated`` is
the only signal that more matches sit past ``--limit``. Every ledger here
is synthetic and built under tmp_path; nothing reads a live database.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ledger.cli import main
from ledger.store import open_ledger
from ledger.verbs import EXIT_OK
from ledger.verbs import search as search_verb

WORD = "widget"
TITLE_CAP = 100
ELLIPSIS = "\u2026"
MORE_LINE = "+more (raise --limit or narrow with --type/--item)"
JSON_DATA_KEYS = {
    "cards",
    "filters",
    "limit",
    "query",
    "ranker",
    "refreshed",
    "truncated",
}


def invoke(monkeypatch, argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _append(lg, kind: str, item_id: str | None, payload: dict, key: str):
    return lg.append(
        kind=kind,
        item_id=item_id,
        payload=payload,
        idempotency_key=key,
        harness="fixture",
    )


def _db_with_titles(tmp_path: Path, monkeypatch, titles: list[tuple[str, str]]) -> Path:
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    db = tmp_path / "state" / "home.db"
    with open_ledger(db) as lg:
        for item_id, title in titles:
            _append(
                lg,
                "item.created",
                item_id,
                {"type": "ticket", "title": title},
                item_id,
            )
    (db.parent / "config.json").write_text(
        json.dumps({"search_roots": []}), encoding="utf-8"
    )
    return db


def _title_columns(text: str) -> list[str]:
    titles = []
    for line in text.splitlines():
        if not line or line.startswith("\t") or line.startswith("+"):
            continue
        titles.append(line.split("\t")[3])
    return titles


def test_capped_title_cuts_at_the_cap_and_ends_with_an_ellipsis():
    long_title = "a" * (TITLE_CAP + 1)
    shown = search_verb._capped_title(long_title)
    assert len(shown) == TITLE_CAP
    assert shown.endswith(search_verb.TITLE_ELLIPSIS)
    assert shown == (
        long_title[: TITLE_CAP - len(search_verb.TITLE_ELLIPSIS)]
        + search_verb.TITLE_ELLIPSIS
    )
    assert search_verb.TITLE_CAP == TITLE_CAP


def test_capped_title_prints_a_title_at_the_cap_whole():
    exact = "b" * TITLE_CAP
    assert search_verb._capped_title(exact) == exact
    assert search_verb.TITLE_ELLIPSIS not in search_verb._capped_title(exact)


def test_more_line_omitted_when_not_cut():
    assert search_verb._more_line(False) is None


def test_more_line_is_plus_more_when_the_count_is_unknown():
    # The index only fetches one extra row, so N past the limit is unknown.
    # Inventing N (including 1) is refused by this assertion.
    shown = search_verb._more_line(True)
    assert shown == MORE_LINE
    assert shown.startswith("+more ")
    assert not shown.startswith("+1 ")


def test_text_keeps_snippet_type_and_id_and_caps_the_title():
    title = "parent-item " + ("x" * 120)
    snippet = "keep the sixteen token snippet"
    text = search_verb._text(
        [
            {
                "score": 1.5,
                "doc_type": "item",
                "id": "parent-item",
                "title": title,
                "snippet": snippet,
            }
        ],
        truncated=False,
    )
    header, body = text.split("\n", 1)
    score, doc_type, doc_id, shown = header.split("\t")
    assert score == "1.500"
    assert doc_type == "item"
    assert doc_id == "parent-item"
    assert len(shown) == TITLE_CAP
    assert shown.endswith(ELLIPSIS)
    assert body == f"\t{snippet}"
    assert MORE_LINE not in text


def test_text_prints_the_more_line_only_when_truncated():
    card = {
        "score": 1.0,
        "doc_type": "item",
        "id": "child-item",
        "title": "short",
        "snippet": "",
    }
    cut = search_verb._text([card], truncated=True)
    assert cut.splitlines()[-1] == MORE_LINE
    whole = search_verb._text([card], truncated=False)
    assert MORE_LINE not in whole
    assert whole.splitlines()[-1] == "1.000\titem\tchild-item\tshort"


def test_default_ten_card_query_caps_titles_and_names_the_cut(tmp_path, monkeypatch):
    titles = []
    for n in range(12):
        titles.append(
            (f"child-item-{n}", f"{WORD} card {n:02d} " + ("y" * 200))
        )
    db = _db_with_titles(tmp_path, monkeypatch, titles)
    code, out, err = invoke(monkeypatch, ["--db", str(db), "search", WORD])
    assert code == EXIT_OK, err
    shown = _title_columns(out)
    assert len(shown) == 10
    assert all(len(title) <= TITLE_CAP for title in shown)
    assert all(title.endswith(ELLIPSIS) for title in shown)
    assert out.splitlines()[-1] == MORE_LINE
    code, raw, err = invoke(
        monkeypatch, ["--json", "--db", str(db), "search", WORD]
    )
    assert code == EXIT_OK, err
    body = json.loads(raw)
    data = body["data"]
    assert set(data) == JSON_DATA_KEYS
    assert data["limit"] == 10
    assert data["truncated"] is True
    assert len(data["cards"]) == 10
    stored = {card["id"]: card["title"] for card in data["cards"]}
    for card in data["cards"]:
        assert len(card["title"]) > TITLE_CAP
        assert "item_id" in card
        assert "date" in card
        assert card["doc_type"] == "item"
        assert card["snippet"]
    for line in out.splitlines():
        if not line or line.startswith("\t") or line.startswith("+"):
            continue
        doc_id = line.split("\t")[2]
        assert stored[doc_id] not in line


def test_json_is_unchanged_when_text_is_capped(tmp_path, monkeypatch):
    title = f"{WORD} " + ("z" * 180)
    db = _db_with_titles(tmp_path, monkeypatch, [("parent-item", title)])
    code, raw, err = invoke(
        monkeypatch, ["--json", "--db", str(db), "search", WORD]
    )
    assert code == EXIT_OK, err
    data = json.loads(raw)["data"]
    assert set(data) == JSON_DATA_KEYS
    assert data["truncated"] is False
    assert data["cards"][0]["title"] == title
    assert data["cards"][0]["item_id"] == "parent-item"
    assert "date" in data["cards"][0]
    code, out, err = invoke(monkeypatch, ["--db", str(db), "search", WORD])
    assert code == EXIT_OK, err
    assert MORE_LINE not in out
    shown = _title_columns(out)
    assert len(shown) == 1
    assert len(shown[0]) == TITLE_CAP
    assert shown[0].endswith(ELLIPSIS)
