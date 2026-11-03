"""Append-only hash-chained SQLite ledger.

BEFORE UPDATE and BEFORE DELETE triggers refuse writes on the events table.
This is an accident guard, not a security boundary: anyone with filesystem
access can drop the triggers or replace the file. Events are validated at
ingestion; ``verify`` re-checks the hash chain and payloads.

Writes use WAL mode, ``busy_timeout``, one short transaction per append, and
bounded retry with jitter. Schema changes after the initial table are
forward-only migrations of views and indexes; events are never rewritten.
A database that records a schema version this reader does not package was
written by a newer schema and is refused on every open path (never migrated,
written or partly read); update the checkout to open it. The read-write open
checks before it changes anything in the file, and every write transaction
checks again under its own write lock, so this code never writes into a
schema that a newer writer migrated after the open.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .decisions import check_event as check_decision_event
from .decisions import decision_records
from .paths import SharedDatabaseError, SyncPathError, SyncPathWarning, resolve_db_path
from .schema import (
    ENTITY_REGISTERED_KIND,
    ENTITY_STATE_KIND,
    SPEC_KIND,
    SYSTEM_CLAIM_KIND,
    ValidationError,
    validate_item_id,
    validate_payload,
)

BUSY_TIMEOUT_MS = 5_000
APPEND_RETRIES = 8
GENESIS_HASH = "0" * 64
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_RECORDED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
RESERVED_KIND_PREFIX = "loop."
# Kinds one writer owns: claim_system writes the system claim, set_spec
# (verbs/spec.py) writes item.spec, and register_entity and set_entity_state
# (verbs/entity.py) write the entity kinds. Generic appends refuse them.
ENTITY_KINDS = (ENTITY_REGISTERED_KIND, ENTITY_STATE_KIND)
RESERVED_KINDS = (SYSTEM_CLAIM_KIND, SPEC_KIND, *ENTITY_KINDS)
RESERVED_NOTE_PREFIXES = ("work:", "loop:")

# Non-Cf members of Unicode's Default_Ignorable_Code_Point property. Cf
# characters are handled from their category so additions in future Unicode
# versions are covered automatically.
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0xE0000, 0xE0FFF),
)


class LedgerError(Exception):
    """A store operation failed."""


class ChainBroken(LedgerError):
    """``verify`` found a prev_hash / hash mismatch or a bad payload on disk."""


class IdempotencyConflict(LedgerError):
    """The idempotency key is already stored with a different event."""


class StaleRevision(LedgerError):
    """The item's newest event is not the one the caller expected.

    ``current`` is the id of the item's newest event (``None`` when it has
    none). The message names that event's actor and session.
    """

    def __init__(self, message: str, *, item_id: str, expected: Any, current: int | None):
        super().__init__(message)
        self.item_id = item_id
        self.expected = expected
        self.current = current


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


@dataclass(frozen=True)
class Event:
    id: int
    kind: str
    payload_version: int
    payload: dict[str, Any]
    item_id: str | None
    idempotency_key: str
    actor: str | None
    harness: str | None
    model: str | None
    session: str | None
    channel: str | None
    provenance_class: str | None
    recorded_at: str
    prev_hash: str
    hash: str


def canonical_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_digest(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_dumps(dict(body)).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _opt_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValidationError(f"bad-type:{name}")
    if value == "":
        return None
    return value


def _is_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "busy" in msg or "locked" in msg


def _is_io_error(exc: sqlite3.OperationalError) -> bool:
    """True for SQLite's I/O error family (``SQLITE_IOERR`` and its extended codes)."""
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and code & 0xFF == sqlite3.SQLITE_IOERR


def _normalized_note_prefix(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    visible = "".join(
        char
        for char in normalized
        if unicodedata.category(char) != "Cf"
        and not any(start <= ord(char) <= end for start, end in _DEFAULT_IGNORABLE_RANGES)
    )
    return " ".join(visible.split())


def load_migrations() -> list[Migration]:
    """Read packaged ``migrations/NNNN_name.sql`` files in version order."""
    found: dict[int, Migration] = {}
    root = Path(__file__).parent / "migrations"
    for entry in root.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in found:
            raise LedgerError(f"duplicate migration version {version:04d}: {entry.name}")
        found[version] = Migration(version, entry.name, entry.read_text(encoding="utf-8"))
    return [found[v] for v in sorted(found)]


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def _refuse_unknown_versions(done: set[int], migrations: Sequence[Migration]) -> None:
    """Raise ``LedgerError`` when ``done`` holds a version this reader does not package.

    Every recorded version must be one of ``migrations``; comparing only
    against the highest packaged version would let a gap through. The
    read-write open (``_refuse_newer_schema_before_connect``, then
    ``_migrate_attempt``), the read-only open (``open_ledger_readonly``) and
    every write transaction (``_refuse_newer_schema_in_transaction``) call it.
    """
    known = {migration.version for migration in migrations}
    unknown = sorted(done - known)
    if not unknown:
        return
    highest = f"{max(known):04d}" if known else "none"
    raise LedgerError(
        "database was written by a newer schema: unknown schema version(s) "
        f"{', '.join(f'{v:04d}' for v in unknown)}; this reader's highest packaged "
        f"version is {highest}; update the checkout before opening it"
    )


def _recorded_versions(conn: sqlite3.Connection) -> set[int]:
    """The versions ``schema_version`` records, read without writing anything.

    Unlike ``applied_versions`` this never creates the table: a database
    without it records no version.
    """
    has_versions = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if has_versions is None:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def _refuse_newer_schema_in_transaction(conn: sqlite3.Connection) -> None:
    """Refuse to write into a schema that a newer writer has migrated.

    Every write transaction calls this right after its ``BEGIN IMMEDIATE``
    and before it writes anything; the caller rolls back on the refusal.
    While that write lock is held no other writer can commit, so a database
    that records only known versions here stays that way until COMMIT. An
    open-time check cannot promise that: a newer writer may migrate the file
    the moment the open returns.
    """
    _refuse_unknown_versions(_recorded_versions(conn), load_migrations())


def _refuse_newer_schema_before_connect(path: Path) -> None:
    """Refuse a database written by a newer schema before anything changes the file.

    ``connect`` switches the journal mode and sets PRAGMAs, so ``open_ledger``
    runs it only after this check passes. The check opens ``mode=rw`` (which
    never creates a missing file), sets no PRAGMA and only reads, so it takes
    no write lock: a cleanly closed database keeps its bytes and its journal
    mode, and SQLite removes any ``-wal``/``-shm`` file it made when the last
    connection closes (``mode=ro`` would leave both beside a WAL database).
    The one change SQLite itself may still make is its standard recovery of a
    crashed writer's hot journal, which restores the last committed state.
    """
    if not path.exists():
        return
    conn = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, isolation_level=None,
                           timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        versions = _recorded_versions(conn)
    finally:
        conn.close()
    _refuse_unknown_versions(versions, load_migrations())


def _script_statements(script: str) -> list[str]:
    """Split a SQLite script so it can run inside an already-open transaction.

    ``Connection.executescript`` issues COMMIT first, which would drop a
    ``BEGIN IMMEDIATE`` lock. Statements are cut with
    ``sqlite3.complete_statement`` so trigger bodies stay intact.
    """
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                statements.append(stmt)
            buf = ""
    tail = buf.strip()
    if tail:
        statements.append(tail)
    return statements


def _migrate_attempt(conn: sqlite3.Connection, migrations: list[Migration]) -> list[str]:
    done = applied_versions(conn)
    _refuse_unknown_versions(done, migrations)
    if all(migration.version in done for migration in migrations):
        return []
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = applied_versions(conn)
        _refuse_unknown_versions(done, migrations)
        applied: list[str] = []
        for migration in migrations:
            if migration.version in done:
                continue
            try:
                for statement in _script_statements(migration.sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                    (migration.version, migration.name),
                )
            except sqlite3.OperationalError as exc:
                if _is_busy(exc):
                    raise
                raise LedgerError(f"migration {migration.name} failed: {exc}") from exc
            except sqlite3.Error as exc:
                raise LedgerError(f"migration {migration.name} failed: {exc}") from exc
            applied.append(migration.name)
            done.add(migration.version)
        conn.execute("COMMIT")
        return applied
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[str]:
    """Apply every migration not yet recorded in ``schema_version``.

    Pending work takes ``BEGIN IMMEDIATE``, re-reads ``schema_version`` inside
    that transaction, and skips versions already applied so concurrent first
    opens do not double-apply. SQLITE_BUSY is retried with jitter up to
    ``APPEND_RETRIES`` times. Each migration and its ``schema_version`` row
    commit in one transaction, so a failing migration leaves the database at
    the previous version. After the initial events table, later migrations
    may touch views and indexes only and must not rewrite events.
    """
    if migrations is None:
        migrations = load_migrations()
    last_error: BaseException | None = None
    for attempt in range(APPEND_RETRIES):
        try:
            return _migrate_attempt(conn, migrations)
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            last_error = exc
            if not _is_busy(exc) or attempt >= APPEND_RETRIES - 1:
                raise LedgerError(f"migration failed: {exc}") from exc
            time.sleep(random.uniform(0.005, 0.025) * (attempt + 1))
    raise LedgerError("migration retries exhausted") from last_error


def connect(path: Path) -> sqlite3.Connection:
    """Open the database with WAL journaling and a busy timeout.

    ``PRAGMA journal_mode = WAL`` runs only when the file is not already WAL.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: BaseException | None = None
    for attempt in range(APPEND_RETRIES):
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            last_error = exc
            if not _is_busy(exc) or attempt >= APPEND_RETRIES - 1:
                raise
            time.sleep(random.uniform(0.005, 0.025) * (attempt + 1))
    raise LedgerError("connect retries exhausted") from last_error


def item_is_loop_managed(conn: sqlite3.Connection, item_id: str) -> bool:
    """True when ``item_id`` carries a ``loop.managed`` event.

    Takes a connection so an ``append`` precondition can check it inside the
    append's own transaction.
    """
    return conn.execute(
        "SELECT 1 FROM events WHERE item_id = ? AND kind = 'loop.managed' LIMIT 1",
        (item_id,),
    ).fetchone() is not None


def _claimed_system(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT payload FROM events WHERE kind = ? ORDER BY id LIMIT 1",
        (SYSTEM_CLAIM_KIND,),
    ).fetchone()
    return None if row is None else json.loads(row["payload"])["system"]


def refuse_second_claim(owner: str | None, claimed: Any, where: str) -> None:
    """Refuse a ``ledger.system`` claim on a database that already has one.

    ``owner`` is the system already on record, or ``None`` for an unclaimed
    database, which the first claim binds. Another system's claim is a
    ``SharedDatabaseError``; a repeat of the same system's claim is refused
    too, so a database carries exactly one claim.
    """
    if owner is None:
        return
    if claimed != owner:
        raise SharedDatabaseError(
            f"systems {owner!r} and {claimed!r} share one database: {where}"
        )
    raise ValidationError(f"system-already-claimed:{owner}")


def check_expected_system(owner: str | None, expected: str, where: str) -> None:
    """Refuse a claim that is not ``expected``'s, for a caller with a target.

    Another system's claim is a ``SharedDatabaseError``. An unclaimed
    database or export fails closed too, as ``system-unclaimed``: under an
    explicit target, nothing on record shows the data is that system's.
    """
    if owner is None:
        raise ValidationError(f"system-unclaimed:{expected}")
    if owner != expected:
        raise SharedDatabaseError(
            f"systems {owner!r} and {expected!r} share one database: {where}"
        )


def check_system_claim(conn: sqlite3.Connection, kind: str, payload: Mapping[str, Any],
                       where: str) -> None:
    """The claim guard every write and every restored row passes."""
    if kind == SYSTEM_CLAIM_KIND:
        refuse_second_claim(_claimed_system(conn), payload.get("system"), where)


def check_claim_sequence(events: Sequence[Event], where: str) -> str | None:
    """Refuse an event sequence that carries more than one system claim.

    Returns the one claimed system, or ``None`` when the sequence has no claim.
    """
    owner: str | None = None
    for event in events:
        if event.kind != SYSTEM_CLAIM_KIND:
            continue
        refuse_second_claim(owner, event.payload.get("system"), f"{where}, event {event.id}")
        owner = event.payload.get("system")
    return owner


def _event_from_row(row: sqlite3.Row) -> Event:
    payload = json.loads(row["payload"])
    if type(payload) is not dict:
        raise ChainBroken(f"event {row['id']}: payload is not an object")
    return Event(
        id=int(row["id"]),
        kind=row["kind"],
        payload_version=int(row["payload_version"]),
        payload=payload,
        item_id=row["item_id"],
        idempotency_key=row["idempotency_key"],
        actor=row["actor"],
        harness=row["harness"],
        model=row["model"],
        session=row["session"],
        channel=row["channel"],
        provenance_class=row["provenance_class"],
        recorded_at=row["recorded_at"],
        prev_hash=row["prev_hash"],
        hash=row["hash"],
    )


def _hash_body(event: Event) -> dict[str, Any]:
    return {
        "actor": event.actor,
        "channel": event.channel,
        "harness": event.harness,
        "id": event.id,
        "idempotency_key": event.idempotency_key,
        "item_id": event.item_id,
        "kind": event.kind,
        "model": event.model,
        "payload": event.payload,
        "payload_version": event.payload_version,
        "prev_hash": event.prev_hash,
        "provenance_class": event.provenance_class,
        "recorded_at": event.recorded_at,
        "session": event.session,
    }


def _same_semantic(
    existing: Event,
    *,
    kind: str,
    payload_version: int,
    payload: Mapping[str, Any],
    item_id: str | None,
    actor: str | None,
    harness: str | None,
    model: str | None,
    session: str | None,
    channel: str | None,
    provenance_class: str | None,
    ignore_attribution: bool = False,
) -> bool:
    return (
        existing.kind == kind
        and existing.payload_version == payload_version
        and canonical_dumps(existing.payload) == canonical_dumps(payload)
        and existing.item_id == item_id
        and (
            ignore_attribution
            or (
                existing.actor == actor
                and existing.harness == harness
                and existing.model == model
                and existing.session == session
                and existing.channel == channel
                and existing.provenance_class == provenance_class
            )
        )
    )


def _one_line(value: str | None) -> str:
    """Render a stored field for a one-line message: ``-`` when empty, else
    every non-printable character (newline, CR, tab, C0/C1 controls, DEL)
    escaped the way ``repr`` writes it; printable text stays as-is."""
    if not value:
        return "-"
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in value)


def _check_expected_revision(
    conn: sqlite3.Connection, item_id: str, expected: int | str
) -> None:
    """Refuse unless ``item_id``'s newest event id is ``expected`` (or ``"none"``)."""
    row = conn.execute(
        "SELECT id, actor, session FROM events WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    current = None if row is None else int(row["id"])
    if (expected == "none" and current is None) or expected == current:
        return
    if row is None:
        detail = "item has no events"
    else:
        detail = (
            f"event {current} written by actor={_one_line(row['actor'])} "
            f"session={_one_line(row['session'])}"
        )
    raise StaleRevision(
        f"stale revision for item {item_id}: expected {expected}, "
        f"current is {'none' if current is None else current} ({detail})",
        item_id=item_id,
        expected=expected,
        current=current,
    )


class Ledger:
    """Open database handle: append, query views, verify the hash chain."""

    def __init__(self, conn: sqlite3.Connection, path: Path):
        self._conn = conn
        self.path = path
        self._in_batch = False

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        item_id: str | None = None,
        payload_version: int = 1,
        actor: str | None = None,
        harness: str | None = None,
        model: str | None = None,
        session: str | None = None,
        channel: str | None = None,
        provenance_class: str | None = None,
        recorded_at: str | None = None,
        precondition: Callable[[sqlite3.Connection], None] | None = None,
        expect_revision: int | str | None = None,
    ) -> Event:
        """Append one validated event, or return the existing row on a retry.

        One short ``BEGIN IMMEDIATE`` transaction covers the tail-hash read and
        the insert. ``SQLITE_BUSY`` is retried with jitter up to
        ``APPEND_RETRIES`` times. ``precondition``, if given, runs inside that
        transaction before the insert; a raised exception rolls back.

        ``expect_revision`` is a compare-and-set on the item: an event id, or
        ``"none"`` for an item with no events. The append lands only when the
        id of the item's newest event equals it, checked in the same
        transaction as the insert; otherwise ``StaleRevision`` is raised and
        nothing is written. A retry of an already-stored key returns the
        stored event without the check. ``None`` skips the check.
        """
        return self._append(
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
            item_id=item_id,
            payload_version=payload_version,
            actor=actor,
            harness=harness,
            model=model,
            session=session,
            channel=channel,
            provenance_class=provenance_class,
            recorded_at=recorded_at,
            precondition=precondition,
            expect_revision=expect_revision,
            internal=False,
            ignore_retry_attribution=False,
        )

    def _append_loop_event(
        self,
        capability: object,
        *,
        ignore_retry_attribution: bool = False,
        **kwargs: Any,
    ) -> Event:
        """Append a reserved event only for the loop writer module."""
        from .loop import _LOOP_WRITER_CAPABILITY

        if capability is not _LOOP_WRITER_CAPABILITY:
            raise ValidationError("loop-writer-capability-required")
        # The loop writer owns loop.* kinds only; claim_system alone writes
        # the system claim and set_spec alone writes item.spec.
        if kwargs.get("kind") in RESERVED_KINDS:
            raise ValidationError(f"reserved-kind:{kwargs['kind']}")
        return self._append(
            internal=True,
            ignore_retry_attribution=ignore_retry_attribution,
            **kwargs,
        )

    def _append_spec_event(self, capability: object, **kwargs: Any) -> Event:
        """Append an ``item.spec`` event only for the spec writer (``set_spec``)."""
        from .verbs.spec import _SPEC_WRITER_CAPABILITY

        if capability is not _SPEC_WRITER_CAPABILITY:
            raise ValidationError("spec-writer-capability-required")
        # The spec writer owns item.spec only.
        if kwargs.get("kind") != SPEC_KIND:
            raise ValidationError(f"reserved-kind:{kwargs.get('kind')}")
        return self._append(internal=True, ignore_retry_attribution=False, **kwargs)

    def _append_entity_event(self, capability: object, **kwargs: Any) -> Event:
        """Append an entity event only for the entity writer (verbs/entity.py)."""
        from .verbs.entity import _ENTITY_WRITER_CAPABILITY

        if capability is not _ENTITY_WRITER_CAPABILITY:
            raise ValidationError("entity-writer-capability-required")
        # The entity writer owns entity.registered and entity.state only.
        if kwargs.get("kind") not in ENTITY_KINDS:
            raise ValidationError(f"reserved-kind:{kwargs.get('kind')}")
        return self._append(internal=True, ignore_retry_attribution=False, **kwargs)

    def append_many(self, events: Sequence[Mapping[str, Any]]) -> list[Event]:
        """Append several events in ONE transaction: all of them or none.

        Each element holds ``append``'s keyword arguments, and each event gets
        exactly ``append``'s validation, idempotency check, loop guard and
        ``precondition``, with the hash chain advanced per event. Any failure
        rolls back every event in the call. ``SQLITE_BUSY`` on opening the
        transaction is retried with jitter up to ``APPEND_RETRIES`` times.
        """
        specs = [dict(spec) for spec in events]
        with self.transaction():
            return [self.append(**spec) for spec in specs]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the block's appends in ONE transaction: all of them or none.

        Inside the block ``append`` keeps its validation, idempotency check
        and preconditions, and reads see the block's earlier appends. Any
        exception rolls back every append in the block. ``SQLITE_BUSY`` on
        opening the transaction is retried with jitter up to
        ``APPEND_RETRIES`` times.
        """
        if self._in_batch:
            raise LedgerError("append_many cannot nest")
        conn = self._conn
        if conn.in_transaction:
            raise LedgerError("append_many needs a connection outside any transaction")
        last_error: BaseException | None = None
        # BEGIN sits inside the try, so any exception after it (an interrupt
        # included) rolls back; the check above makes the transaction ours.
        try:
            for attempt in range(APPEND_RETRIES):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    last_error = exc
                    if not _is_busy(exc) or attempt >= APPEND_RETRIES - 1:
                        raise
                    time.sleep(random.uniform(0.005, 0.025) * (attempt + 1))
            else:
                raise LedgerError("append retries exhausted") from last_error
            _refuse_newer_schema_in_transaction(conn)
            self._in_batch = True
            yield
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            self._in_batch = False

    def _append(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        item_id: str | None = None,
        payload_version: int = 1,
        actor: str | None = None,
        harness: str | None = None,
        model: str | None = None,
        session: str | None = None,
        channel: str | None = None,
        provenance_class: str | None = None,
        recorded_at: str | None = None,
        precondition: Callable[[sqlite3.Connection], None] | None = None,
        expect_revision: int | str | None = None,
        ignore_retry_attribution: bool,
        internal: bool,
    ) -> Event:
        if type(kind) is not str or not kind:
            raise ValidationError("unknown-kind:")
        if internal:
            if not kind.startswith(RESERVED_KIND_PREFIX) and kind not in RESERVED_KINDS:
                raise ValidationError(f"internal-kind-required:{kind}")
        elif kind.startswith(RESERVED_KIND_PREFIX) or kind in RESERVED_KINDS:
            raise ValidationError(f"reserved-kind:{kind}")
        if type(idempotency_key) is not str or not idempotency_key:
            raise ValidationError("missing-key:idempotency_key")
        if type(payload_version) is not int:
            raise ValidationError("bad-type:payload_version")
        payload_obj = validate_payload(kind, dict(payload), payload_version)
        item = validate_item_id(kind, item_id, payload_version)
        if not internal and kind == "item.note":
            normalized_note = _normalized_note_prefix(payload_obj["text"])
            if normalized_note.startswith(RESERVED_NOTE_PREFIXES):
                raise ValidationError("reserved-note-prefix")
        actor = _opt_str(actor, "actor")
        harness = _opt_str(harness, "harness")
        model = _opt_str(model, "model")
        session = _opt_str(session, "session")
        channel = _opt_str(channel, "channel")
        provenance_class = _opt_str(provenance_class, "provenance_class")
        if recorded_at is None:
            recorded_at = _now()
        elif type(recorded_at) is not str or not _RECORDED_AT.match(recorded_at):
            raise ValidationError("bad-type:recorded_at")
        payload_text = canonical_dumps(payload_obj)
        try:
            payload_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationError("bad-type:payload") from exc
        if expect_revision is not None:
            if item is None:
                raise ValidationError("expect-revision-needs-item")
            if expect_revision != "none" and (
                type(expect_revision) is not int or expect_revision < 1
            ):
                raise ValidationError("bad-type:expect_revision")
        caller_precondition = precondition

        def checked_precondition(conn: sqlite3.Connection) -> None:
            if expect_revision is not None:
                _check_expected_revision(conn, item, expect_revision)
            if not internal and item is not None:
                managed = conn.execute(
                    "SELECT 1 FROM events WHERE item_id = ? AND kind = 'loop.managed' LIMIT 1",
                    (item,),
                ).fetchone()
                if managed is not None:
                    if kind == "item.state":
                        loop_closed = conn.execute(
                            "SELECT 1 FROM events "
                            "WHERE item_id = ? AND kind = 'loop.closed' LIMIT 1",
                            (item,),
                        ).fetchone()
                        if payload_obj["status"] == "closed" or loop_closed is not None:
                            raise ValidationError("reserved-transition:loop-close")
                    if kind == "item.linked" and payload_obj["rel"] == "pr":
                        raise ValidationError("reserved-transition:loop-pr")
            # The standing guard lives here, on the one write path every
            # writer takes, not in a CLI verb.
            check_decision_event(conn, kind, payload_obj, item)
            # One system claim per database, whichever path writes it.
            check_system_claim(conn, kind, payload_obj, str(self.path))
            if caller_precondition is not None:
                caller_precondition(conn)

        last_error: BaseException | None = None
        for attempt in range(APPEND_RETRIES):
            try:
                return self._append_once(
                    kind=kind,
                    payload=payload_obj,
                    payload_version=payload_version,
                    item_id=item,
                    idempotency_key=idempotency_key,
                    actor=actor,
                    harness=harness,
                    model=model,
                    session=session,
                    channel=channel,
                    provenance_class=provenance_class,
                    recorded_at=recorded_at,
                    payload_text=payload_text,
                    precondition=checked_precondition,
                    ignore_retry_attribution=ignore_retry_attribution,
                )
            except sqlite3.OperationalError as exc:
                last_error = exc
                if not _is_busy(exc) or attempt >= APPEND_RETRIES - 1:
                    raise
                time.sleep(random.uniform(0.005, 0.025) * (attempt + 1))
        raise LedgerError("append retries exhausted") from last_error

    def _append_once(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        payload_version: int,
        item_id: str | None,
        idempotency_key: str,
        actor: str | None,
        harness: str | None,
        model: str | None,
        session: str | None,
        channel: str | None,
        provenance_class: str | None,
        recorded_at: str,
        payload_text: str,
        precondition: Callable[[sqlite3.Connection], None] | None,
        ignore_retry_attribution: bool,
    ) -> Event:
        conn = self._conn
        # Inside append_many the batch owns the transaction.
        owned = not self._in_batch
        if owned and conn.in_transaction:
            raise LedgerError("append needs a connection outside any transaction")
        # BEGIN sits inside the try, so any exception after it (an interrupt
        # included) rolls back; the check above makes the transaction ours.
        try:
            if owned:
                conn.execute("BEGIN IMMEDIATE")
                # Inside a batch, transaction() made this check under the same lock.
                _refuse_newer_schema_in_transaction(conn)
            existing = conn.execute(
                "SELECT * FROM events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                event = _event_from_row(existing)
                if not _same_semantic(
                    event,
                    kind=kind,
                    payload_version=payload_version,
                    payload=payload,
                    item_id=item_id,
                    actor=actor,
                    harness=harness,
                    model=model,
                    session=session,
                    channel=channel,
                    provenance_class=provenance_class,
                    ignore_attribution=ignore_retry_attribution,
                ):
                    raise IdempotencyConflict(
                        f"idempotency_key {idempotency_key!r} already recorded as a different event"
                    )
                if owned:
                    conn.execute("COMMIT")
                return event
            if precondition is not None:
                precondition(conn)
            tail = conn.execute("SELECT id, hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            if tail is None:
                next_id = 1
                prev_hash = GENESIS_HASH
            else:
                next_id = int(tail["id"]) + 1
                prev_hash = tail["hash"]
            body = {
                "actor": actor,
                "channel": channel,
                "harness": harness,
                "id": next_id,
                "idempotency_key": idempotency_key,
                "item_id": item_id,
                "kind": kind,
                "model": model,
                "payload": payload,
                "payload_version": payload_version,
                "prev_hash": prev_hash,
                "provenance_class": provenance_class,
                "recorded_at": recorded_at,
                "session": session,
            }
            digest = event_digest(body)
            conn.execute(
                """INSERT INTO events (
                    id, kind, payload_version, payload, item_id, idempotency_key,
                    actor, harness, model, session, channel, provenance_class,
                    recorded_at, prev_hash, hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    next_id,
                    kind,
                    payload_version,
                    payload_text,
                    item_id,
                    idempotency_key,
                    actor,
                    harness,
                    model,
                    session,
                    channel,
                    provenance_class,
                    recorded_at,
                    prev_hash,
                    digest,
                ),
            )
            if owned:
                conn.execute("COMMIT")
            return Event(
                id=next_id,
                kind=kind,
                payload_version=payload_version,
                payload=payload,
                item_id=item_id,
                idempotency_key=idempotency_key,
                actor=actor,
                harness=harness,
                model=model,
                session=session,
                channel=channel,
                provenance_class=provenance_class,
                recorded_at=recorded_at,
                prev_hash=prev_hash,
                hash=digest,
            )
        except BaseException:
            if owned and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def system(self) -> str | None:
        """The system this database is claimed for, or ``None`` if unclaimed."""
        return _claimed_system(self._conn)

    def claim_system(self, system: str) -> None:
        """Bind this database to ``system``, or refuse another system's.

        The claim is a ``ledger.system`` event, so it lives in the database
        file itself: every path that reaches the file (a link, a hard link,
        another spelling) sees it, and an export or restore carries it. The
        check and the write share one ``BEGIN IMMEDIATE`` transaction, so an
        interrupted claim leaves the database unclaimed. An unclaimed
        database, including one from before claims existed, is claimed by
        the first system to install it. Another system's claim is refused by
        the claim guard on the shared write path, as ``SharedDatabaseError``.

        A database reached through a second path while a connection on the
        first path has unsaved WAL frames can fail with a raw SQLite I/O
        error; that, and a checkpoint that cannot finish, are reported as
        ``SharedDatabaseError`` too. Any other SQLite failure, such as a
        read-only file, is refused as a ``LedgerError`` that names it.
        """
        try:
            with self.transaction():
                if _claimed_system(self._conn) != system:
                    self._append(
                        kind=SYSTEM_CLAIM_KIND,
                        payload={"system": system},
                        idempotency_key=f"{SYSTEM_CLAIM_KIND}:{system}",
                        internal=True,
                        ignore_retry_attribution=False,
                    )
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise
            if not _is_io_error(exc):
                raise LedgerError(
                    f"system {system!r}: could not claim {self.path}: {exc}"
                ) from exc
            raise SharedDatabaseError(
                f"system {system!r}: {self.path} could not be read ({exc}); "
                "another path may share this database"
            ) from exc
        # Fold the claim into the main file, whose WAL another path's
        # connection would not read. A checkpoint that cannot finish means
        # another connection, perhaps through a hard link to this file,
        # still holds the database; the claim is then not settled on disk.
        busy, wal_frames, done = self._conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if busy:
            raise SharedDatabaseError(
                f"system {system!r}: checkpoint of {self.path} is busy "
                f"({done} of {wal_frames} WAL frames written back): another "
                "connection, perhaps through another path to this file, holds the database"
            )

    def verify(self, *, system: str | None = None) -> None:
        """Walk events by id and refuse a broken hash chain or a bad payload.

        A bare hash chain cannot detect tail truncation: deleting the last
        event(s) leaves the remaining prefix valid. The exported, pushed
        commit is the anchor for that.

        A chain that carries more than one system claim is refused: another
        system's claim as ``SharedDatabaseError``, a repeated one as
        ``ValidationError``. With ``system``, the chain must carry that
        system's claim: another system's lone claim is a
        ``SharedDatabaseError`` and an unclaimed chain is refused as
        ``system-unclaimed`` (see ``check_expected_system``).
        """
        prev = GENESIS_HASH
        owner: str | None = None
        for row in self._conn.execute("SELECT * FROM events ORDER BY id ASC"):
            event = _event_from_row(row)
            if event.prev_hash != prev:
                raise ChainBroken(
                    f"event {event.id}: prev_hash {event.prev_hash} != {prev}"
                )
            expected = event_digest(_hash_body(event))
            if event.hash != expected:
                raise ChainBroken(f"event {event.id}: hash mismatch")
            validate_payload(event.kind, event.payload, event.payload_version)
            validate_item_id(event.kind, event.item_id, event.payload_version)
            if event.kind == SYSTEM_CLAIM_KIND:
                refuse_second_claim(owner, event.payload["system"],
                                    f"{self.path}, event {event.id}")
                owner = event.payload["system"]
            prev = event.hash
        if system is not None:
            check_expected_system(owner, system, str(self.path))

    def events(
        self,
        *,
        item_id: str | None = None,
        kind: str | None = None,
        after_id: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [after_id]
        if item_id is not None:
            sql += " AND item_id = ?"
            params.append(item_id)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_event_from_row(row) for row in self._conn.execute(sql, params)]

    def items(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM v_items")]

    def items_with_revision(self) -> list[dict[str, Any]]:
        """Like ``items()``, plus ``revision``: the id of each item's newest event.

        One statement, so the projection and the revision come from the same
        snapshot; two reads could straddle a concurrent append.
        """
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT v.*, (SELECT MAX(e.id) FROM events e "
                "WHERE e.item_id = v.item_id) AS revision FROM v_items v"
            )
        ]

    def event_by_key(self, idempotency_key: str) -> Event | None:
        """Return the event stored under ``idempotency_key``, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM events WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return None if row is None else _event_from_row(row)

    def item(self, item_id: str) -> dict[str, Any] | None:
        """Return one ``v_items`` row, or ``None`` when the item does not exist."""
        row = self._conn.execute(
            "SELECT * FROM v_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def is_loop_managed(self, item_id: str) -> bool:
        """True when the item loop owns this item's state."""
        return item_is_loop_managed(self._conn, item_id)

    def open_decisions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM v_open_decisions")]

    def decision_records(
        self,
        *,
        item_id: str | None = None,
        tag: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Decision records with their current standing (see ``decisions``)."""
        return decision_records(self._conn, item_id=item_id, tag=tag)

    def ready(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM v_ready")]

    def blocked(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM v_blocked")]


def open_ledger(path: Path | str, *, create: bool = True) -> Ledger:
    """Resolve ``path``, refuse synced roots, migrate, and return a handle.

    A database written by a newer schema is refused before ``connect`` changes
    the file. After ``connect`` returns, the version is read again and refused
    the same way, before migrate or any other write. A fully migrated database
    returns without taking the write lock, so a newer writer may still migrate
    it after this returns; every write then re-checks under its own lock
    (``_refuse_newer_schema_in_transaction``).
    """
    resolved = resolve_db_path(path)
    if not create and not resolved.exists():
        raise LedgerError(f"database does not exist: {resolved}")
    _refuse_newer_schema_before_connect(resolved)
    conn = connect(resolved)
    try:
        _refuse_unknown_versions(_recorded_versions(conn), load_migrations())
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return Ledger(conn, resolved)


def claim_database(db: Path | str, system: str) -> Path:
    """Open ``db``, claim it for ``system`` (see ``Ledger.claim_system``), close."""
    with open_ledger(db) as lg:
        lg.claim_system(system)
        return lg.path


# Files SQLite keeps beside a database while any connection to it is open (or
# after a crash). Their absence proves no connection is open.
_SIDE_FILE_SUFFIXES = ("-wal", "-shm", "-journal")


def _side_files_present(path: Path) -> bool:
    return any(Path(f"{path}{suffix}").exists() for suffix in _SIDE_FILE_SUFFIXES)


def _is_readonly_refusal(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) == sqlite3.SQLITE_READONLY
    return "readonly" in str(exc).lower()


def _readonly_conn(uri: str) -> sqlite3.Connection:
    conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                           timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Connect ``mode=ro``; in a directory we cannot write, fall back to ``immutable=1``.

    Opening a WAL database ``mode=ro`` needs the ``-wal`` and ``-shm`` files.
    Measured on SQLite 3.53.4: a writable directory gets both created, and
    they stay after the reader closes (the database bytes never change); a
    read-only directory fails the first query with "attempt to write a
    readonly database". The files in a writable directory are SQLite's own
    coordination state, and ``mode=ro`` is the only open that stays correct
    while a writer is active (it reads through the WAL), so that case keeps
    ``mode=ro`` and accepts the files.

    ``immutable=1`` skips locking and the WAL, so it would give stale or torn
    reads beside a live writer. It is used only when ``mode=ro`` was refused
    as read-only and no ``-wal``, ``-shm`` or ``-journal`` file exists: a
    connection that is open or was open leaves one of them, so their absence
    is a filesystem check that no writer holds the database, and a directory
    we cannot write is where we could not have created them ourselves.
    Residual: a writer that can write the directory (another user, or the
    directory made writable meanwhile) could start between the check and the
    read; that window is accepted, since the alternative is a reader that
    cannot open the ledger at all.
    """
    uri = path.as_uri()
    conn = _readonly_conn(f"{uri}?mode=ro")
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except BaseException as exc:
        conn.close()
        refused = (isinstance(exc, sqlite3.OperationalError)
                   and _is_readonly_refusal(exc))
        if not refused or _side_files_present(path):
            raise
    return _readonly_conn(f"{uri}?immutable=1")


def open_ledger_readonly(path: Path | str) -> Ledger:
    """Open a fully migrated ledger without changing the database.

    A writable directory may gain SQLite's ``-wal`` and ``-shm`` files (the
    database bytes are unchanged); a read-only directory with no such files
    is read ``immutable``. See ``_connect_readonly`` for the measurements and
    why a live writer is never read that way.
    """
    resolved = resolve_db_path(path)
    if not resolved.exists():
        raise LedgerError(f"database does not exist: {resolved}")
    conn = _connect_readonly(resolved)
    try:
        has_versions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        versions = ({row[0] for row in conn.execute("SELECT version FROM schema_version")}
                    if has_versions else set())
        packaged = load_migrations()
        _refuse_unknown_versions(versions, packaged)
        missing = [migration.name for migration in packaged
                   if migration.version not in versions]
        if missing:
            raise LedgerError(f"database needs migrations: {', '.join(missing)}")
    except BaseException:
        conn.close()
        raise
    return Ledger(conn, resolved)


__all__ = [
    "APPEND_RETRIES",
    "BUSY_TIMEOUT_MS",
    "ChainBroken",
    "Event",
    "GENESIS_HASH",
    "IdempotencyConflict",
    "Ledger",
    "LedgerError",
    "Migration",
    "RESERVED_KIND_PREFIX",
    "RESERVED_NOTE_PREFIXES",
    "SyncPathError",
    "StaleRevision",
    "SyncPathWarning",
    "ValidationError",
    "canonical_dumps",
    "check_claim_sequence",
    "check_expected_system",
    "check_system_claim",
    "claim_database",
    "connect",
    "event_digest",
    "item_is_loop_managed",
    "load_migrations",
    "migrate",
    "open_ledger",
    "open_ledger_readonly",
]
