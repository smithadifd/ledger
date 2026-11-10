"""A derived full-text index over the ledger and its markdown roots.

The index is ``search.db`` in the ledger database's own directory. It is a
disposable projection, never a source: ``home.db`` stays events only (ADR
0034 decision 1), the index is rebuilt from scratch whenever anything but the
event log's tail changed (``refresh_index``), and nothing exports or commits
it. The ledger is opened with ``open_ledger_readonly``
(``mode=ro``; ``immutable=1`` only when no WAL sidecar exists), so building
the index never writes the ledger database.

What is indexed, one FTS5 row per document:

- ``item``: each current item (``v_items``) with its notes and links.
- ``decision``: each ``decision.asked`` and ``decision.elected`` event.
- ``utterance``: each ``utterance.captured`` event whose payload says
  ``origin: person``, and no other.
- ``standard`` / ``adr``: ``standards/*.md`` and ``decisions/*.md`` under each
  configured root. A folder derived from a root (``<root>/standards``,
  ``<root>/decisions``) must resolve inside that root, so a symlinked folder
  that points outside is skipped. A folder named by an explicit standards or
  decisions setting in the configuration is indexed at its own path, even
  outside the root. Files inside either kind of folder
  stay confined to that folder.
- ``doc``: a markdown path named in an item or decision payload, when it
  resolves to a file inside a configured root.

Markdown is chunked at H2: one row per ``## `` section, plus one for the text
before the first H2. A section's ``doc_id`` is ``<path>#<anchor>``.

Roots come from ``search_roots`` (a list of absolute paths) in
``config.json`` beside the database. Without that key the root is whatever
the caller's ``fallback_root`` returns for that config; the ledger command
supplies the shared root resolver there, so this package names no personal
root and never discovers repositories. With neither, only the ledger is
indexed and the result says so.

Every document also carries structured fields beside its text: the item it
belongs to (an item, a decision asked or elected on it, a person utterance
captured on it), that item's current status, and a date. The date is the
recorded time of the event for a decision or an utterance, the recorded time
of the item's newest event for an item, and the file's mtime for markdown;
all in UTC. ``search`` filters on these (``status``, ``item``,
``initiative``, ``since``, ``until``) in the same query as the text match.
``initiative`` keeps the documents of items with an ``item.linked`` event
whose ``rel`` is ``item`` and whose target is that initiative. A filter on an
item field drops every document that has no item, such as a standard.

The index stores a manifest: the format, the ranker, the last event id and
its hash, the roots, and every source file with its mtime and size. ``index_status``
recomputes it and calls the index stale on any difference, so an edited,
added, deleted or newly linked source is noticed; ``search`` rebuilds a stale
index before it reads, filtered or not. The only ranker is BM25 (``RANKER``);
it is named in the manifest so a second ranker can be added later and an
index built by another, or by a version that named none, reads as stale.

When the only difference is events appended after the build (``behind``),
``refresh_index`` brings the index up to date without re-reading any markdown:
on a copy of the index, it recomputes the ledger's documents from one read
snapshot, deletes the indexed ledger documents that no longer match, inserts
the new ones, and swaps the copy in. The markdown side is kept as it is, which
is correct only because the manifest's sources (paths, mtimes and sizes),
roots, format, ranker and database all still match. Anything else, or any
error on the way, takes the full rebuild instead.

``index_status`` also gives a ``severity`` a reader can colour by: ``ok`` for
``current``; ``info`` for ``behind``, ``stale`` or ``missing``, which the next
search or ``ledger index`` brings up to date on its own; ``warn`` when the
index cannot be built where it is (inside a git work tree, a directory at the
index path, an index path that cannot be examined, an unusable configuration),
with that reason added to ``reasons``. Any other status is ``warn``.

The index is opened only when ``lstat`` shows a regular file at its path. A
FIFO, socket, device, directory or symlink there is never opened (a read-only
SQLite open blocks on a FIFO): ``index_status`` calls it ``missing`` and names
it, and a rebuild renames the new index over it, or refuses by name when the
rename cannot replace it (a directory).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .paths import resolve_db_path
from .schema import ITEM_STATUSES
from .store import Event, open_ledger_readonly

SEARCH_DB_NAME = "search.db"
CONFIG_NAME = "config.json"
# Format 2: doc_type became UNINDEXED, so an index built at format 1 is stale.
# Format 3: the doc_meta and item_link tables behind the search filters.
INDEX_FORMAT = 3
# The one ranker. The manifest records it, so an index built by any other
# ranker (or by a version that recorded none) is stale and gets rebuilt.
RANKER = "bm25"
DOC_TYPES = ("item", "decision", "utterance", "standard", "adr", "doc")
# The document types built from the event log; the rest come from markdown.
LEDGER_DOC_TYPES = ("item", "decision", "utterance")
# How a reader colours an index status. ``ok``: current. ``info``: the next
# search or ``ledger index`` brings it up to date on its own. Any status not
# named here (``unknown`` included) is ``warn``.
SEVERITIES = {"current": "ok", "behind": "info", "stale": "info", "missing": "info"}
UTTERANCE_CAVEAT = "recalled words are evidence, never an origin"
MAX_LIMIT = 1000
# One weight per column: doc_type, doc_id, title, content. The two
# UNINDEXED columns never match, so their 0.0 weights only keep the array
# one entry per column.
RANK_SQL = "bm25(fts_corpus, 0.0, 0.0, 10.0, 1.0)"
SCHEMA_SQL = (
    "CREATE VIRTUAL TABLE fts_corpus USING fts5("
    # doc_type is filtered by equality (--type), never matched: an indexed
    # doc_type made a word like "standards" match every standard row.
    "doc_type UNINDEXED, doc_id UNINDEXED, title, content, "
    "tokenize='porter unicode61 remove_diacritics 1')",
    "CREATE TABLE doc_path (rowid INTEGER PRIMARY KEY, path TEXT NOT NULL)",
    # One row per document: the item it belongs to, that item's status, and
    # the date the --since/--until filters compare, as ISO text and as
    # microseconds since the epoch (UTC).
    "CREATE TABLE doc_meta (rowid INTEGER PRIMARY KEY, item_id TEXT, status TEXT, "
    "date TEXT, ts INTEGER)",
    # item.linked rel=item: the item belongs to the initiative named by target.
    "CREATE TABLE item_link (item_id TEXT NOT NULL, initiative TEXT NOT NULL, "
    "PRIMARY KEY (item_id, initiative))",
    "CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
SNIPPET_TOKENS = 16
UTTERANCE_TITLE_CHARS = 80
_LINK_KIND_PREFIXES = ("item.", "decision.")
_MD_PATH = re.compile(r"(?:~/|/)?(?:[\w.@+-]+/)*[\w.@+-]+\.md(?![\w-])")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_TERM = re.compile(r"[\w][\w'-]*", re.UNICODE)
_BARE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_STOPWORDS = frozenset(
    "a about an and are as at be by did do does for from how i in is it of on "
    "or that the this to was we were what when where which who why will with".split()
)


class SearchError(Exception):
    """The index cannot be built or read; the message says why."""


class IndexUnavailable(SearchError):
    """Nothing at the index path can be opened: it is absent, or it is not a
    regular file and so is never opened. ``index_status`` reads this as
    ``missing``, so the next search or ``ledger index`` builds a fresh index
    in its place; the message names what is there."""


@dataclass(frozen=True)
class Root:
    path: Path
    standards: Path
    decisions: Path
    # True when the folder was named by explicit configuration rather than
    # derived from ``path``; only a derived folder is confined to the root.
    standards_configured: bool = False
    decisions_configured: bool = False


# Given the system's config, return its default root, or raise LookupError
# whose message says how to set one.
FallbackRoot = Callable[[Mapping[str, Any]], Root]


@dataclass(frozen=True)
class Doc:
    doc_type: str
    doc_id: str
    title: str
    content: str
    path: str | None = None
    # The structured fields: the owning item, its current status, and the
    # date the date filters compare (ISO text and epoch microseconds).
    item_id: str | None = None
    status: str | None = None
    date: str | None = None
    ts: int | None = None


# --- locations and configuration ------------------------------------------


def search_db_path(db: Path | str) -> Path:
    """``search.db`` in the resolved ledger database's directory."""
    return resolve_db_path(db).parent / SEARCH_DB_NAME


def config_file(db: Path | str) -> Path:
    return resolve_db_path(db).parent / CONFIG_NAME


def load_config(db: Path | str) -> Mapping[str, Any]:
    """The system's ``config.json`` beside the database; ``{}`` when absent."""
    path = config_file(db)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SearchError(f"unreadable ledger config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SearchError(f"ledger config {path} must be a JSON object")
    return data


def resolve_roots(
    config: Mapping[str, Any], fallback_root: FallbackRoot | None = None
) -> tuple[list[Root], str | None]:
    """Return the configured roots, and a note when there are none."""
    if "search_roots" in config:
        raw = config["search_roots"]
        if not isinstance(raw, list) or any(not isinstance(v, str) or not v for v in raw):
            raise SearchError("search_roots must be a list of absolute paths")
        roots: list[Root] = []
        for value in raw:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise SearchError(f"search_roots entry is not absolute: {value}")
            path = path.resolve()
            if all(path != root.path for root in roots):
                roots.append(Root(path, path / "standards", path / "decisions"))
        return roots, None if roots else "search_roots is empty"
    if fallback_root is None:
        return [], "search_roots is not set in the ledger's config.json"
    try:
        return [fallback_root(config)], None
    except LookupError as exc:
        return [], str(exc)


def _inside_git(directory: Path) -> bool:
    return any((parent / ".git").exists() for parent in (directory, *directory.parents))


def refuse_inside_git(db: Path | str) -> None:
    """Refuse a ledger directory inside a git work tree, where search.db could
    be committed. ``index`` and ``search`` call this before anything else, so
    an index that became committable after it was built is refused too."""
    directory = search_db_path(db).parent
    if _inside_git(directory):
        raise SearchError(f"refusing to use {SEARCH_DB_NAME} inside a git work tree: {directory}")


# --- the ledger side --------------------------------------------------------


def _read_ledger(db: Path) -> tuple[int, list[dict[str, Any]], list[Event]]:
    """One read snapshot: last event id, current items, every event."""
    with open_ledger_readonly(db) as lg:
        conn = lg._conn
        conn.execute("BEGIN")
        try:
            items = lg.items()
            events = lg.events()
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
    last = events[-1].id if events else 0
    return last, items, events


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def linked_refs(events: Iterable[Event]) -> list[str]:
    """Markdown paths named anywhere in an item or decision payload."""
    refs: set[str] = set()
    for event in events:
        if not event.kind.startswith(_LINK_KIND_PREFIXES):
            continue
        for text in _strings(event.payload):
            refs.update(_MD_PATH.findall(text))
    return sorted(refs)


def is_person_utterance(event: Event) -> bool:
    return event.kind == "utterance.captured" and event.payload.get("origin") == "person"


def _join(parts: Iterable[Any]) -> str:
    return "\n".join(str(part) for part in parts if part not in (None, ""))


def _micros(moment: datetime) -> int:
    """Whole microseconds since the epoch for an aware datetime."""
    delta = moment - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _event_micros(recorded_at: Any) -> int | None:
    """An event's recorded time in epoch microseconds; None when unparseable."""
    try:
        moment = datetime.fromisoformat(recorded_at)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _micros(moment)


def newest_event_times(
    events: Iterable[tuple[str | None, Any]],
) -> dict[str, tuple[int, str]]:
    """Each item's newest event time, by recorded time, from (item_id, recorded_at).

    An item's date is its newest event of any kind; events with no item or an
    unparseable time are skipped, and on a tie the first event wins.
    """
    newest: dict[str, tuple[int, str]] = {}
    for item_id, recorded_at in events:
        if item_id is None:
            continue
        micros = _event_micros(recorded_at)
        if micros is not None and (item_id not in newest or micros > newest[item_id][0]):
            newest[item_id] = (micros, recorded_at)
    return newest


def in_window(micros: int | None, since: datetime | None, until: datetime | None) -> bool:
    """True when ``micros`` lies in the inclusive ``since``..``until`` window.

    No bound keeps everything; with a bound, a document with no date is out,
    as in the index's ``ts >= ? AND ts <= ?`` filter.
    """
    if since is None and until is None:
        return True
    if micros is None:
        return False
    if since is not None and micros < _micros(since):
        return False
    return until is None or micros <= _micros(until)


def _mtime_date(mtime_ns: int) -> tuple[str, int]:
    micros = mtime_ns // 1000
    moment = _EPOCH + timedelta(microseconds=micros)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z"), micros


def initiative_links(events: Iterable[Event]) -> set[tuple[str, str]]:
    """``(item_id, initiative)`` for each ``item.linked`` event with rel=item."""
    links: set[tuple[str, str]] = set()
    for event in events:
        if event.kind != "item.linked" or event.item_id is None:
            continue
        target = event.payload.get("target")
        if event.payload.get("rel") == "item" and isinstance(target, str) and target:
            links.add((event.item_id, target))
    return links


def ledger_docs(items: Sequence[Mapping[str, Any]], events: Sequence[Event]) -> list[Doc]:
    statuses = {row["item_id"]: row["status"] for row in items}
    # An item's date is its newest event by recorded time, any kind.
    newest = newest_event_times((event.item_id, event.recorded_at) for event in events)
    notes: dict[str, list[str]] = {}
    for event in events:
        if event.item_id is None:
            continue
        if event.kind == "item.note":
            notes.setdefault(event.item_id, []).append(event.payload.get("text", ""))
        elif event.kind == "item.linked":
            notes.setdefault(event.item_id, []).append(
                f"{event.payload.get('rel')}: {event.payload.get('target')}"
            )
    corpus = [
        Doc(
            "item",
            row["item_id"],
            row["title"] or row["item_id"],
            _join([f"{row['type']} {row['status']}", *notes.get(row["item_id"], [])]),
            item_id=row["item_id"],
            status=row["status"],
            date=newest[row["item_id"]][1] if row["item_id"] in newest else None,
            ts=newest[row["item_id"]][0] if row["item_id"] in newest else None,
        )
        for row in items
    ]
    for event in events:
        payload = event.payload
        meta = {
            "item_id": event.item_id,
            "status": statuses.get(event.item_id) if event.item_id is not None else None,
            "date": event.recorded_at,
            "ts": _event_micros(event.recorded_at),
        }
        if event.kind == "decision.asked":
            options = [
                _join([opt.get("label"), opt.get("description")])
                for opt in payload.get("options", [])
                if isinstance(opt, Mapping)
            ]
            corpus.append(Doc(
                "decision",
                f"{event.item_id}:{event.id}",
                payload.get("question", ""),
                _join([
                    payload.get("recommendation"), payload.get("default"),
                    *options, payload.get("ask"), payload.get("unblocks"),
                    payload.get("body"),
                ]),
                **meta,
            ))
        elif event.kind == "decision.elected":
            corpus.append(Doc(
                "decision",
                f"{event.item_id}:{event.id}",
                f"elected: {payload.get('question', '')}",
                _join([
                    payload.get("chosen"), payload.get("chosen_description"),
                    *payload.get("rejected", []), payload.get("scope"),
                    *payload.get("quotes", []),
                ]),
                **meta,
            ))
        elif is_person_utterance(event):
            text = payload.get("text", "")
            first = text.strip().splitlines()[0] if text.strip() else ""
            corpus.append(Doc(
                "utterance",
                f"event:{event.id}",
                first[:UTTERANCE_TITLE_CHARS],
                text,
                **meta,
            ))
    return corpus


# --- the markdown side ------------------------------------------------------


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_ref(ref: str, roots: Sequence[Root]) -> Path | None:
    """A linked path as a file inside a configured root, or None."""
    raw = Path(ref).expanduser()
    if raw.is_absolute():
        candidates = [raw]
    else:
        candidates = []
        for root in roots:
            candidates.append(root.path / raw)
            # "<root folder>/standards/x.md" names the root by its folder name.
            if raw.parts and raw.parts[0] == root.path.name:
                candidates.append(root.path.parent / raw)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.suffix == ".md" and resolved.is_file() and any(
            _within(resolved, root.path) for root in roots
        ):
            return resolved
    return None


def collect_sources(roots: Sequence[Root], refs: Iterable[str]) -> dict[Path, str]:
    """Every markdown source, mapped to its doc_type, in a stable order."""
    sources: dict[Path, str] = {}
    for root in roots:
        for folder, doc_type, configured in (
            (root.standards, "standard", root.standards_configured),
            (root.decisions, "adr", root.decisions_configured),
        ):
            if not folder.is_dir():
                continue
            confined = folder.resolve()
            # A derived folder that resolves outside its root (a directory
            # symlink) is not globbed at all; a configured one is indexed
            # where it lives.
            if not configured and not _within(confined, root.path):
                continue
            for path in sorted(folder.glob("*.md")):
                resolved = path.resolve()
                # A symlink is confined to its own folder, which is stricter
                # than the root-wide rule for linked paths.
                if resolved.is_file() and _within(resolved, confined):
                    sources.setdefault(resolved, doc_type)
    for ref in refs:
        resolved = _resolve_ref(ref, roots)
        if resolved is not None:
            sources.setdefault(resolved, "doc")
    return sources


def _display_path(path: Path, roots: Sequence[Root]) -> str:
    for root in roots:
        if _within(path, root.path):
            return path.relative_to(root.path).as_posix()
    return path.as_posix()


def slugify(heading: str) -> str:
    """A GitHub-style heading anchor."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower(), flags=re.UNICODE)
    return re.sub(r"\s", "-", text)


def chunk_markdown(text: str, fallback_title: str) -> list[tuple[str, str, str]]:
    """Split at H2 outside code fences: (anchor, title, body) per chunk.

    The first chunk is the text before the first H2, with anchor ``""``; it
    is kept when it has text or when there is no H2 at all.
    """
    doc_title = fallback_title
    chunks: list[tuple[str, str | None, list[str]]] = [("", None, [])]
    # Every anchor already given out, so "Notes", "Notes-1", "Notes" yields
    # three distinct ids; "" is the text before the first H2.
    used: set[str] = {""}
    # The open fence as (character, length). Per CommonMark, it closes only
    # on the same character, at least as long, with nothing after it.
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match:
            run, rest = match.group(1), match.group(2)
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not rest.strip():
                fence = None
            chunks[-1][2].append(line)
            continue
        if fence is None:
            if line.startswith("# ") and doc_title == fallback_title:
                doc_title = line[2:].strip().strip("#").strip() or fallback_title
            if line.startswith("## "):
                heading = line[3:].strip().strip("#").strip()
                base = slugify(heading)
                anchor, count = base, 0
                while anchor in used:
                    count += 1
                    anchor = f"{base}-{count}"
                used.add(anchor)
                chunks.append((anchor, heading, []))
                continue
        chunks[-1][2].append(line)
    result: list[tuple[str, str, str]] = []
    for index, (anchor, heading, lines) in enumerate(chunks):
        body = "\n".join(lines).strip()
        if index == 0:
            if body or len(chunks) == 1:
                result.append(("", doc_title, body))
            continue
        result.append((anchor, f"{doc_title} § {heading}", body))
    return result


def markdown_docs(
    path: Path, doc_type: str, roots: Sequence[Root], mtime_ns: int | None = None
) -> list[Doc]:
    """One Doc per H2 chunk; dated by ``mtime_ns`` (the manifest's stat)."""
    shown = _display_path(path, roots)
    text = path.read_text(encoding="utf-8", errors="replace")
    when, micros = _mtime_date(mtime_ns) if mtime_ns is not None else (None, None)
    return [
        Doc(
            doc_type, f"{shown}#{anchor}" if anchor else shown, title, body, str(path),
            date=when, ts=micros,
        )
        for anchor, title, body in chunk_markdown(text, path.stem)
    ]


# --- manifest ---------------------------------------------------------------


def _stat(path: Path) -> list[int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return [info.st_mtime_ns, info.st_size]


def _manifest(
    db: Path, last: int, events: Sequence[Event], roots: Sequence[Root], sources: Mapping[Path, str]
) -> dict:
    # The final event's hash identifies the ledger's content; an empty ledger has none.
    last_hash = events[-1].hash if events else ""
    stats = {str(path): _stat(path) for path in sources}
    return {
        "db": str(db),
        "format": INDEX_FORMAT,
        "last_event_hash": last_hash,
        "last_event_id": last,
        "ranker": RANKER,
        "roots": [str(root.path) for root in roots],
        "sources": {path: stat for path, stat in stats.items() if stat is not None},
    }


def current_manifest(db: Path | str, fallback_root: FallbackRoot | None = None) -> dict:
    resolved = resolve_db_path(db)
    roots, _note = resolve_roots(load_config(resolved), fallback_root)
    last, _items, events = _read_ledger(resolved)
    return _manifest(resolved, last, events, roots, collect_sources(roots, linked_refs(events)))


# lstat file kinds other than a regular file, in the words the refusals use.
_NOT_REGULAR_KINDS = (
    (stat.S_ISLNK, "symbolic link"),
    (stat.S_ISDIR, "directory"),
    (stat.S_ISFIFO, "FIFO"),
    (stat.S_ISSOCK, "socket"),
    (stat.S_ISCHR, "character device"),
    (stat.S_ISBLK, "block device"),
)


def _not_regular_kind(mode: int) -> str | None:
    """``None`` for a regular file, else the kind of file ``mode`` describes."""
    if stat.S_ISREG(mode):
        return None
    for test, kind in _NOT_REGULAR_KINDS:
        if test(mode):
            return kind
    return "non-regular file"


def _lstat_index(index: Path) -> os.stat_result | None:
    """The index path's own ``lstat`` (a symlink is not followed), or ``None``
    when nothing is there. Any other ``lstat`` failure refuses by name."""
    try:
        return os.lstat(index)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SearchError(f"cannot stat the search index {index}: {exc}") from exc


def _open_index(index: Path) -> sqlite3.Connection:
    """Open the index read-only, and only when its path is a regular file.

    A ``mode=ro`` open blocks on a FIFO instead of failing, so the path is
    checked with ``lstat`` first and anything but a regular file (a FIFO,
    socket, device, directory, or a symlink, which is not followed) is never
    opened. The index is derived and decides nothing, so a name check is
    enough; there is no descriptor identity check.
    """
    found = _lstat_index(index)
    if found is None:
        raise IndexUnavailable(f"no {SEARCH_DB_NAME}")
    kind = _not_regular_kind(found.st_mode)
    if kind is not None:
        raise IndexUnavailable(f"{SEARCH_DB_NAME} is a {kind}, not a regular file: {index}")
    return sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)


def stored_manifest(index: Path) -> dict:
    conn = _open_index(index)
    try:
        row = conn.execute("SELECT value FROM manifest WHERE key = 'manifest'").fetchone()
    finally:
        conn.close()
    if row is None:
        raise SearchError("index has no manifest")
    return json.loads(row[0])


def trailing_events(stored: Mapping[str, Any], events: Sequence[Event]) -> tuple[int | None, str]:
    """Compare the index's recorded event position with the live event log.

    Returns ``(new_events, reason)``. ``new_events`` is how many events were
    appended after the index was built (0 when none), or ``None`` when the log
    no longer matches what the index recorded: the recorded id is past the end,
    or the event at that id has a different hash (a rewritten or truncated
    log), or the record is unreadable. ``reason`` then says why. Anything
    outside the clean-prefix case fails closed to ``None``.
    """
    last = stored.get("last_event_id")
    recorded = stored.get("last_event_hash")
    if type(last) is not int or last < 0:
        return None, "last_event_id changed"
    if not isinstance(recorded, str):
        return None, "last_event_hash changed"
    if last == 0:
        # An index of an empty ledger recorded no hash.
        return (len(events), "") if recorded == "" else (None, "last_event_hash changed")
    at_id = next((event for event in events if event.id == last), None)
    if at_id is None:
        return None, "last_event_id changed"
    if at_id.hash != recorded:
        return None, "last_event_hash changed"
    return sum(1 for event in events if event.id > last), ""


def manifest_differences(stored: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    reasons = [
        f"{key} changed"
        for key in ("format", "ranker", "db", "roots")
        if stored.get(key) != current.get(key)
    ]
    old = stored.get("sources") or {}
    new = current.get("sources") or {}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    for label, paths in (("added", added), ("removed", removed), ("changed", changed)):
        if paths:
            reasons.append(f"{len(paths)} source(s) {label}: {paths[0]}")
    return reasons


def severity_for(status: str) -> str:
    """``ok``, ``info`` or ``warn`` for an index status; an unnamed status is ``warn``."""
    return SEVERITIES.get(status, "warn")


def _build_blocker(
    resolved: Path, index: Path, fallback_root: FallbackRoot | None
) -> str | None:
    """Why building the index at ``index`` would refuse, or ``None``."""
    if _inside_git(index.parent):
        return f"refusing to use {SEARCH_DB_NAME} inside a git work tree: {index.parent}"
    try:
        found = _lstat_index(index)
        if found is not None and stat.S_ISDIR(found.st_mode):
            # A rename cannot replace a directory. Worded as the missing
            # reason is, so ``index_status`` names it once.
            return f"{SEARCH_DB_NAME} is a directory, not a regular file: {index}"
        resolve_roots(load_config(resolved), fallback_root)
    except SearchError as exc:
        return str(exc)
    return None


def index_status(db: Path | str, fallback_root: FallbackRoot | None = None) -> dict[str, Any]:
    """``missing``, ``stale`` (with reasons), ``behind`` or ``current``, and a
    ``severity``. Writes nothing.

    ``behind`` means the index matches everything it recorded and only newer
    events exist; ``stale`` is a real mismatch (format, ranker, database, roots,
    source files, or an event log that no longer matches what was indexed).
    ``severity`` is ``severity_for(status)``, or ``warn`` when a rebuild would
    refuse, with that reason in ``reasons`` (added unless already there). An
    index whose state cannot be computed because the configuration is unusable
    reads ``unknown`` (so ``warn``) with the reason, instead of raising.
    """
    resolved = resolve_db_path(db)
    index = search_db_path(resolved)
    try:
        result = _index_state(resolved, index, fallback_root)
    except SearchError as exc:
        result = {"path": str(index), "status": "unknown", "reasons": [str(exc)]}
    blocker = _build_blocker(resolved, index, fallback_root)
    if blocker is None:
        result["severity"] = severity_for(result["status"])
    else:
        if blocker not in result["reasons"]:
            result["reasons"] = [*result["reasons"], blocker]
        result["severity"] = "warn"
    return result


def _index_state(
    resolved: Path, index: Path, fallback_root: FallbackRoot | None
) -> dict[str, Any]:
    try:
        stored = stored_manifest(index)
    except IndexUnavailable as exc:
        # Absent, or not a regular file (never opened): a rebuild replaces it.
        return {"path": str(index), "status": "missing", "reasons": [str(exc)]}
    except (sqlite3.Error, ValueError, SearchError) as exc:
        return {"path": str(index), "status": "stale", "reasons": [f"unreadable index: {exc}"]}
    roots, _note = resolve_roots(load_config(resolved), fallback_root)
    last, _items, events = _read_ledger(resolved)
    current = _manifest(resolved, last, events, roots, collect_sources(roots, linked_refs(events)))
    reasons = manifest_differences(stored, current)
    new_events, event_reason = trailing_events(stored, events)
    if new_events is None:
        reasons.append(event_reason)
    if reasons:
        return {"path": str(index), "status": "stale", "reasons": reasons}
    if new_events:
        # Events appended after the build are information, not a mismatch: the
        # next search indexes them. ``search`` still rebuilds before answering.
        return {
            "path": str(index),
            "status": "behind",
            "reasons": [],
            "indexed_to_event": stored["last_event_id"],
            "new_events": new_events,
        }
    return {"path": str(index), "status": "current", "reasons": []}


# --- build ------------------------------------------------------------------


def _insert_doc(conn: sqlite3.Connection, rowid: int, doc: Doc) -> None:
    conn.execute(
        "INSERT INTO fts_corpus (rowid, doc_type, doc_id, title, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (rowid, doc.doc_type, doc.doc_id, doc.title, doc.content),
    )
    if doc.path is not None:
        conn.execute("INSERT INTO doc_path (rowid, path) VALUES (?, ?)", (rowid, doc.path))
    conn.execute(
        "INSERT INTO doc_meta (rowid, item_id, status, date, ts) VALUES (?, ?, ?, ?, ?)",
        (rowid, doc.item_id, doc.status, doc.date, doc.ts),
    )


def _delete_doc(conn: sqlite3.Connection, rowid: int) -> None:
    for table in ("fts_corpus", "doc_path", "doc_meta"):
        conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))


def build_index(db: Path | str, fallback_root: FallbackRoot | None = None) -> dict[str, Any]:
    """Rebuild ``search.db`` from scratch and swap it in atomically."""
    resolved = resolve_db_path(db)
    index = search_db_path(resolved)
    refuse_inside_git(resolved)
    roots, note = resolve_roots(load_config(resolved), fallback_root)
    last, items, events = _read_ledger(resolved)
    sources = collect_sources(roots, linked_refs(events))
    # Stat before reading: a file edited mid-build reads as stale next time.
    manifest = _manifest(resolved, last, events, roots, sources)
    corpus = ledger_docs(items, events)
    links = initiative_links(events)
    for path, doc_type in sources.items():
        stat = manifest["sources"].get(str(path))
        try:
            corpus.extend(markdown_docs(
                path, doc_type, roots, mtime_ns=stat[0] if stat else None))
        except OSError:
            manifest["sources"].pop(str(path), None)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{SEARCH_DB_NAME}.", suffix=".partial", dir=index.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        conn = sqlite3.connect(tmp)
        try:
            for statement in SCHEMA_SQL:
                conn.execute(statement)
            for rowid, doc in enumerate(corpus, start=1):
                _insert_doc(conn, rowid, doc)
            conn.executemany(
                "INSERT INTO item_link (item_id, initiative) VALUES (?, ?)", sorted(links)
            )
            conn.execute(
                "INSERT INTO manifest (key, value) VALUES ('manifest', ?)",
                (json.dumps(manifest, sort_keys=True),),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            # A rename replaces a FIFO, socket or symlink at the index path
            # (the link itself, never its target); it cannot replace a
            # directory, which is refused by name.
            os.replace(tmp, index)
        except OSError as exc:
            found = _lstat_index(index)
            kind = None if found is None else _not_regular_kind(found.st_mode)
            if kind is None:
                raise
            raise SearchError(
                f"cannot replace the {kind} at the search index path {index}: {exc}"
            ) from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    counts: dict[str, int] = {}
    for doc in corpus:
        counts[doc.doc_type] = counts.get(doc.doc_type, 0) + 1
    return {
        "path": str(index),
        "docs": len(corpus),
        "by_type": dict(sorted(counts.items())),
        "last_event_id": last,
        "roots": manifest["roots"],
        "sources": len(manifest["sources"]),
        "note": note,
    }


# --- refresh ----------------------------------------------------------------


def _indexed_ledger_docs(conn: sqlite3.Connection) -> list[tuple[int, Doc]]:
    """Every ledger document in the index, with its rowid."""
    rows = conn.execute(
        "SELECT fts_corpus.rowid, fts_corpus.doc_type, fts_corpus.doc_id, "
        "fts_corpus.title, fts_corpus.content, doc_path.path, doc_meta.item_id, "
        "doc_meta.status, doc_meta.date, doc_meta.ts FROM fts_corpus "
        "LEFT JOIN doc_path ON doc_path.rowid = fts_corpus.rowid "
        "LEFT JOIN doc_meta ON doc_meta.rowid = fts_corpus.rowid "
        f"WHERE fts_corpus.doc_type IN ({', '.join('?' for _ in LEDGER_DOC_TYPES)})",
        LEDGER_DOC_TYPES,
    ).fetchall()
    return [(row[0], Doc(*row[1:])) for row in rows]


def _append_into(
    conn: sqlite3.Connection, resolved: Path, fallback_root: FallbackRoot | None
) -> dict[str, Any] | None:
    """Bring the index open on ``conn`` (a private copy) up to the ledger's
    tail. ``None`` when the copy is anything but behind: the caller rebuilds.

    The stored manifest is read from the copy itself, so the check and the
    write see the same index.
    """
    row = conn.execute("SELECT value FROM manifest WHERE key = 'manifest'").fetchone()
    if row is None:
        return None
    stored = json.loads(row[0])
    if not isinstance(stored, dict):
        return None
    roots, note = resolve_roots(load_config(resolved), fallback_root)
    last, items, events = _read_ledger(resolved)
    sources = collect_sources(roots, linked_refs(events))
    current = _manifest(resolved, last, events, roots, sources)
    new_events, _reason = trailing_events(stored, events)
    # Only the clean-prefix case with something new; any other difference
    # (format, ranker, database, roots, a source, a rewritten log) rebuilds.
    if manifest_differences(stored, current) or not new_events:
        return None
    # The ledger side is recomputed exactly as a full build would, and the
    # index is made to hold that multiset: documents the tail changed (an
    # item's notes, status or date; the status on its questions) are deleted
    # and re-inserted, new ones are inserted, the rest stay.
    wanted = Counter(ledger_docs(items, events))
    for rowid, doc in _indexed_ledger_docs(conn):
        if wanted[doc] > 0:
            wanted[doc] -= 1
        else:
            _delete_doc(conn, rowid)
    rowid = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM fts_corpus").fetchone()[0]
    for doc in wanted.elements():
        rowid += 1
        _insert_doc(conn, rowid, doc)
    conn.execute("DELETE FROM item_link")
    conn.executemany(
        "INSERT INTO item_link (item_id, initiative) VALUES (?, ?)",
        sorted(initiative_links(events)),
    )
    conn.execute(
        "UPDATE manifest SET value = ? WHERE key = 'manifest'",
        (json.dumps(current, sort_keys=True),),
    )
    counts = dict(conn.execute(
        "SELECT doc_type, COUNT(*) FROM fts_corpus GROUP BY doc_type ORDER BY doc_type"
    ).fetchall())
    return {
        "docs": sum(counts.values()),
        "by_type": counts,
        "last_event_id": last,
        "roots": current["roots"],
        "sources": len(current["sources"]),
        "note": note,
        "refresh": "append",
        "new_events": new_events,
    }


def _append_trailing(
    resolved: Path, index: Path, fallback_root: FallbackRoot | None
) -> dict[str, Any] | None:
    """Append the ledger's tail to a behind index on a copy and swap the copy
    in, as ``build_index`` swaps a new index in. ``None`` when there is no
    index to start from or it is not behind."""
    try:
        source = _open_index(index)
    except SearchError:
        return None
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{SEARCH_DB_NAME}.", suffix=".partial", dir=index.parent
        )
        os.close(fd)
        tmp = Path(tmp_name)
        conn = sqlite3.connect(tmp)
        try:
            source.backup(conn)
            source.close()
            summary = _append_into(conn, resolved, fallback_root)
            if summary is None:
                return None
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, index)
        tmp = None
    finally:
        source.close()
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return {"path": str(index), **summary}


def refresh_index(db: Path | str, fallback_root: FallbackRoot | None = None) -> dict[str, Any]:
    """Bring ``search.db`` up to date: append the ledger's tail to a behind
    index, otherwise rebuild it from scratch.

    The summary is ``build_index``'s plus ``refresh`` (``append`` or
    ``rebuild``); an append also gives ``new_events``. Any doubt or error in
    the append takes the full rebuild, which reports its own errors, so a
    partial index is never swapped in as current.
    """
    resolved = resolve_db_path(db)
    refuse_inside_git(resolved)
    try:
        summary = _append_trailing(resolved, search_db_path(resolved), fallback_root)
    except Exception:  # fail closed: the full rebuild decides
        summary = None
    if summary is not None:
        return summary
    return {**build_index(resolved, fallback_root), "refresh": "rebuild"}


# --- query ------------------------------------------------------------------


def match_expression(query: str) -> str:
    """Free text to an FTS5 expression: each word quoted, joined with OR.

    Common English words are dropped unless nothing else is left. A
    hyphenated word stays one quoted phrase, so ``feed-condenser`` matches
    the two words in order.
    """
    terms = _TERM.findall(query)
    kept = [term for term in terms if term.lower() not in _STOPWORDS] or terms
    seen: list[str] = []
    for term in kept:
        if term.lower() not in (t.lower() for t in seen):
            seen.append(term)
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in seen)


def parse_when(text: str, *, end_of_day: bool = False) -> datetime:
    """An ISO 8601 date or date-time as an aware UTC datetime.

    A bare date (``2026-10-31``) is midnight UTC, or with ``end_of_day`` the
    last microsecond of that day, so ``--until 2026-10-31`` keeps the whole
    day. A date-time (``2026-10-31T09:30``, optional seconds, fraction and
    ``Z`` or ``+HH:MM`` offset) without an offset is read as UTC. Anything
    else (compact ``20261031``, week or ordinal dates) raises ``ValueError``,
    as does one that falls outside the representable range; the caller
    reports it as a usage error, never as an empty result.
    """
    value = text.strip()
    if _BARE_DATE.match(value):
        day = date.fromisoformat(value)
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return start + timedelta(days=1, microseconds=-1) if end_of_day else start
    if not _DATE_TIME.match(value):
        raise ValueError(f"not YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS][Z|+HH:MM]: {value!r}")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def search(
    db: Path | str,
    query: str,
    *,
    limit: int,
    doc_type: str | None = None,
    status: str | None = None,
    item: str | None = None,
    initiative: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    fallback_root: FallbackRoot | None = None,
) -> dict[str, Any]:
    """Refresh the index when it is not current, then rank hits.

    The filters narrow the text match in the same query; ``since`` and
    ``until`` are inclusive and must be timezone-aware.
    """
    expression = match_expression(query)
    if not expression:
        raise SearchError("the query has no searchable words")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise SearchError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    if status is not None and status not in ITEM_STATUSES:
        raise SearchError(f"status must be one of {', '.join(ITEM_STATUSES)}")
    for name, bound in (("since", since), ("until", until)):
        if bound is not None and (not isinstance(bound, datetime) or bound.tzinfo is None):
            raise SearchError(f"{name} must be a timezone-aware datetime")
    if since is not None and until is not None and since > until:
        raise SearchError("since is after until")
    resolved = resolve_db_path(db)
    refuse_inside_git(resolved)
    # Named apart from the status filter, which it would otherwise replace.
    index_state = index_status(resolved, fallback_root)
    refreshed = index_state["status"] != "current"
    if refreshed:
        try:
            refresh_index(resolved, fallback_root)
        except (OSError, sqlite3.Error) as exc:
            # Never answer from the out-of-date index it could not replace.
            raise SearchError(
                f"cannot rebuild the search index {search_db_path(resolved)}: {exc}"
            ) from exc
    index = search_db_path(resolved)
    sql = (
        "SELECT fts_corpus.doc_type, fts_corpus.doc_id, fts_corpus.title, "
        f"snippet(fts_corpus, 3, '[', ']', '…', {SNIPPET_TOKENS}), "
        f"{RANK_SQL} AS rank, doc_path.path, "
        "doc_meta.item_id, doc_meta.status, doc_meta.date "
        "FROM fts_corpus LEFT JOIN doc_path ON doc_path.rowid = fts_corpus.rowid "
        "LEFT JOIN doc_meta ON doc_meta.rowid = fts_corpus.rowid "
        "WHERE fts_corpus MATCH ?"
    )
    params: list[Any] = [expression]
    filters: dict[str, str] = {}
    if doc_type is not None:
        sql += " AND fts_corpus.doc_type = ?"
        params.append(doc_type)
        filters["type"] = doc_type
    if status is not None:
        sql += " AND doc_meta.status = ?"
        params.append(status)
        filters["status"] = status
    if item is not None:
        sql += " AND doc_meta.item_id = ?"
        params.append(item)
        filters["item"] = item
    if initiative is not None:
        sql += " AND doc_meta.item_id IN (SELECT item_id FROM item_link WHERE initiative = ?)"
        params.append(initiative)
        filters["initiative"] = initiative
    if since is not None:
        sql += " AND doc_meta.ts >= ?"
        params.append(_micros(since))
        filters["since"] = _iso(since.astimezone(UTC))
    if until is not None:
        sql += " AND doc_meta.ts <= ?"
        params.append(_micros(until))
        filters["until"] = _iso(until.astimezone(UTC))
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit + 1)
    conn = _open_index(index)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise SearchError(f"search failed: {exc}") from exc
    finally:
        conn.close()
    cards = []
    for kind, doc_id, title, snippet, rank, path, item_id, item_status, when in rows[:limit]:
        card: dict[str, Any] = {
            "score": round(-rank, 4),
            "doc_type": kind,
            "id": doc_id,
            "title": title,
            "snippet": " ".join(snippet.split()),
        }
        if path is not None:
            card["path"] = path
        for key, value in (("item_id", item_id), ("status", item_status), ("date", when)):
            if value is not None:
                card[key] = value
        if kind == "utterance":
            card["caveat"] = UTTERANCE_CAVEAT
        cards.append(card)
    return {
        "cards": cards,
        "filters": filters,
        "limit": limit,
        "query": query,
        "ranker": RANKER,
        "refreshed": refreshed,
        "truncated": len(rows) > limit,
    }
