"""A log of searches that missed, and the replay that ranks each one again.

``ledger search-miss`` appends one line to the misses file of its database,
beside it: the database's own file name plus ``.search-misses.jsonl`` (for
``home.db``, ``home.db.search-misses.jsonl``). Two databases in one folder keep
two logs, so a replay never reads another database's misses. ``ledger
search-audit`` reads that file and prints the rank each logged miss gets from
the current search index. Neither changes ranking or the index: the log verb
performs one append and nothing else, and the replay only reads.

A miss names one query and one thing that query should have surfaced: an
event (``event:N``, a document built from event N) or an item (its id, the
item's own document). The grammar is fixed, and anything else is refused by
name before anything is written, or, in the file, before any rank is printed.

The misses file, one JSON object per line, ASCII, each line ending in LF:

    {"expected":"event:12","query":"harness map","recorded_at":"2026-11-20T09:30:00Z","v":1}

``note`` is optional. The keys are exactly ``v`` (1), ``recorded_at``
(``YYYY-MM-DDTHH:MM:SSZ``), ``query``, ``expected`` and ``note``; a duplicate
or unknown key, a blank line, a line that is not JSON, text that is not UTF-8
and a last line with no LF are each refused by name, and the replay then
reports itself unverifiable and prints no rank.

Concurrent writers: each append is one ``write`` on a descriptor opened with
``O_APPEND``, so two seats logging at once each add a whole line and neither
replaces the other. A write that stops part-way leaves a line with no LF, which
the replay names; a rerun of the verb appends again (a repeated line is
harmless, the replay ranks it twice), and nothing is ever rewritten. The next
append would continue a torn last line, so the replay's refusal names the line
to remove first.

Every failure of the append after the file is open (a size or quota limit, a
full disk, an I/O error, a failed close) is a ``MissError`` named
``misses-file-write-failed``, never a bare ``OSError``; the verb turns it into
an exit 3 with a JSON envelope. A rerun appends again.
"""
from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .paths import resolve_db_path
from .search import MAX_LIMIT, FallbackRoot, match_expression, search

# The misses file is the database file's own name plus this suffix, so each
# database in a shared folder has its own log.
MISSES_SUFFIX = ".search-misses.jsonl"
FORMAT = 1
# The rank a default ``ledger search`` shows: a miss that ranks past it, or not
# at all, is a candidate golden case.
DEFAULT_RANK = 10
MAX_QUERY_CHARS = 500
MAX_NOTE_CHARS = 500
MAX_EXPECTED_CHARS = 200
_MAX_EVENT_ID = 2**63 - 1  # SQLite integer ceiling
_EVENT_FORM = re.compile(r"event:[1-9][0-9]*")
_RECORDED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
REQUIRED_KEYS = ("v", "recorded_at", "query", "expected")
ALLOWED_KEYS = (*REQUIRED_KEYS, "note")
# The document types built from an event, whose id ends in that event's id.
EVENT_DOC_TYPES = ("decision", "spec", "stop", "utterance")
# A platform without O_NOFOLLOW or O_NONBLOCK (Windows) still imports and runs;
# it just has no protection against a symlink or a FIFO at the misses path.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _NONBLOCK
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK


class MissError(Exception):
    """A refusal with a stable name (``reason``) and a sentence saying what."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class Miss:
    line: int
    recorded_at: str
    query: str
    expected: str
    note: str | None = None


def misses_path(db: Path | str) -> Path:
    """The misses file of one database: its file name plus ``MISSES_SUFFIX``."""
    resolved = resolve_db_path(db)
    return resolved.with_name(resolved.name + MISSES_SUFFIX)


def _has_control(text: str) -> bool:
    """A control character, a Unicode line or paragraph separator, or a
    bidirectional control: the code points the ledger refuses in item text."""
    return any(
        unicodedata.category(char) in ("Cc", "Zl", "Zp")
        or "‪" <= char <= "‮"
        or "⁦" <= char <= "⁩"
        for char in text
    )


# --- the grammar ---------------------------------------------------------------


def check_query(text: str) -> str:
    """The query, trimmed; refused when empty, too long, holding a control
    character, or with no word the search would match."""
    query = text.strip()
    if not query:
        raise MissError("query-empty", "the query is empty")
    if len(query) > MAX_QUERY_CHARS:
        raise MissError("query-too-long", f"the query is over {MAX_QUERY_CHARS} characters")
    if _has_control(query):
        raise MissError("query-control-character", "the query holds a control character")
    if not match_expression(query):
        raise MissError("query-no-searchable-words", "the query has no searchable words")
    return query


def check_expected(text: str) -> str:
    """``event:N`` (N a positive integer, no leading zero) or an item id.

    An item id holds no whitespace, control character or colon; a colon is
    reserved for the ``event:N`` form.
    """
    if not text:
        raise MissError("expected-empty", "the expected event or item is empty")
    if len(text) > MAX_EXPECTED_CHARS:
        raise MissError(
            "expected-too-long", f"the expected event or item is over {MAX_EXPECTED_CHARS} characters"
        )
    if any(char.isspace() or _has_control(char) for char in text):
        raise MissError(
            "expected-bad-character", "the expected event or item holds whitespace or a control character"
        )
    if ":" in text and (_EVENT_FORM.fullmatch(text) is None or int(text[6:]) > _MAX_EVENT_ID):
        raise MissError(
            "expected-bad-form",
            "a colon is allowed only in event:N, N a positive integer with no leading zero; "
            "otherwise give an item id",
        )
    return text


def check_note(text: str) -> str:
    if len(text) > MAX_NOTE_CHARS:
        raise MissError("note-too-long", f"the note is over {MAX_NOTE_CHARS} characters")
    if _has_control(text):
        raise MissError("note-control-character", "the note holds a control character")
    return text


def build_entry(
    query: str, expected: str, note: str | None, now: datetime | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "v": FORMAT,
        "recorded_at": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query": check_query(query),
        "expected": check_expected(expected),
    }
    if note:
        entry["note"] = check_note(note)
    return entry


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise MissError("duplicate-key", f"the key {key!r} appears twice")
        found[key] = value
    return found


def parse_line(text: str) -> dict[str, Any]:
    """One line of the misses file as an entry, or a refusal naming the form."""
    if not text.strip():
        raise MissError("blank-line", "the line is empty")
    try:
        found = json.loads(text, object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError) as exc:
        raise MissError("not-json", f"the line is not JSON: {exc}") from exc
    if type(found) is not dict:
        raise MissError("not-an-object", "the line is not a JSON object")
    for key in found:
        if key not in ALLOWED_KEYS:
            raise MissError("unknown-key", f"the key {key!r} is not one of {', '.join(ALLOWED_KEYS)}")
    for key in REQUIRED_KEYS:
        if key not in found:
            raise MissError("missing-key", f"the key {key!r} is missing")
    if type(found["v"]) is not int or found["v"] != FORMAT:
        raise MissError("bad-version", f"v must be {FORMAT}")
    for key in ALLOWED_KEYS[1:]:
        if key in found and type(found[key]) is not str:
            raise MissError("bad-type", f"{key} is not text")
    if _RECORDED_AT.fullmatch(found["recorded_at"]) is None:
        raise MissError("bad-recorded-at", "recorded_at is not YYYY-MM-DDTHH:MM:SSZ")
    entry = {
        "v": FORMAT,
        "recorded_at": found["recorded_at"],
        "query": check_query(found["query"]),
        "expected": check_expected(found["expected"]),
    }
    if "note" in found:
        entry["note"] = check_note(found["note"])
    return entry


# --- the one append ------------------------------------------------------------


def append_miss(
    db: Path | str,
    query: str,
    expected: str,
    note: str | None = None,
    now: datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Append one entry to the misses file and do nothing else.

    The entry is checked first, so a refusal writes nothing. The file is
    created (mode 0600) when absent, never followed through a symlink, and
    only written when it is a regular file.
    """
    entry = build_entry(query, expected, note, now)
    path = misses_path(db)
    line = (json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    try:
        fd = os.open(path, _APPEND_FLAGS, 0o600)
    except OSError as exc:
        raise MissError("misses-file-unwritable", f"cannot append to {path}: {exc}") from exc
    try:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MissError("misses-file-not-regular", f"{path} is not a regular file")
            written = os.write(fd, line)
        except OSError as exc:
            raise MissError("misses-file-write-failed", f"cannot append to {path}: {exc}") from exc
        if written != len(line):
            raise MissError("misses-file-short-write", f"the append to {path} stopped part-way")
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            raise MissError("misses-file-write-failed", f"cannot close {path}: {exc}") from exc
    return path, entry


# --- the replay ----------------------------------------------------------------


def read_misses(db: Path | str) -> list[Miss]:
    """Every logged miss, in file order; none when there is no file.

    Any line off the grammar, a file that is not a regular file or cannot be
    read, refuses the whole read: nothing is returned that was not checked.
    """
    path = misses_path(db)
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise MissError("misses-file-unreadable", f"cannot read {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MissError("misses-file-not-regular", f"{path} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read()
    except OSError as exc:
        raise MissError("misses-file-unreadable", f"cannot read {path}: {exc}") from exc
    finally:
        os.close(fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MissError("not-utf8", f"{path.name} is not UTF-8: {exc}") from exc
    if text and not text.endswith("\n"):
        raise MissError(
            "unterminated-line",
            f"{path.name} line {text.count(chr(10)) + 1} has no line feed at its end",
        )
    misses = []
    for number, line in enumerate(text.split("\n")[:-1], start=1):
        try:
            entry = parse_line(line)
        except MissError as exc:
            raise MissError(exc.reason, f"{path.name} line {number}: {exc.detail}") from exc
        misses.append(
            Miss(number, entry["recorded_at"], entry["query"], entry["expected"], entry.get("note"))
        )
    return misses


def expected_matches(expected: str, card: Mapping[str, Any]) -> bool:
    """Whether a search card is the document ``expected`` names.

    ``event:N``: a document built from event N (its id ends in ``:N``).
    An item id: that item's own document, not its decisions, specs or notes.
    """
    if expected.startswith("event:"):
        return (
            card["doc_type"] in EVENT_DOC_TYPES
            and card["id"].rsplit(":", 1)[-1] == expected[len("event:"):]
        )
    return card["doc_type"] == "item" and card["id"] == expected


def rank_of(cards: Sequence[Mapping[str, Any]], expected: str) -> int | None:
    """The 1-based position of the first card that is ``expected``, else None."""
    for position, card in enumerate(cards, start=1):
        if expected_matches(expected, card):
            return position
    return None


def is_candidate(rank: int | None) -> bool:
    """A miss still worth a golden case: past the default ten, or not ranked."""
    return rank is None or rank > DEFAULT_RANK


def replay(
    db: Path | str,
    misses: Sequence[Miss],
    fallback_root: FallbackRoot | None = None,
) -> list[dict[str, Any]]:
    """The rank each miss gets now, from an index that is current.

    Reads only: ``search`` is called with ``refresh=False``, so an index that
    is not current raises ``IndexNotCurrent`` instead of being rebuilt. A rank
    of None means the document is not among the first ``MAX_LIMIT`` hits.
    """
    rows = []
    for miss in misses:
        found = search(
            db, miss.query, limit=MAX_LIMIT, fallback_root=fallback_root, refresh=False
        )
        rank = rank_of(found["cards"], miss.expected)
        row: dict[str, Any] = {
            "line": miss.line,
            "recorded_at": miss.recorded_at,
            "query": miss.query,
            "expected": miss.expected,
            "rank": rank,
            "candidate": is_candidate(rank),
        }
        if miss.note is not None:
            row["note"] = miss.note
        if row["candidate"]:
            row["golden_case"] = {
                "query": miss.query,
                "expected": miss.expected,
                "max_rank": DEFAULT_RANK,
            }
        rows.append(row)
    return rows
