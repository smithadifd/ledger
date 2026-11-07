"""Replay an export directory into a fresh ledger database.

Restore inserts the original ``id``, ``hash``, ``prev_hash`` and
``recorded_at`` values. It does not re-hash and does not drop the
append-only UPDATE/DELETE triggers. The replayed chain must then pass
``verify``. For loop-owned exports, the stream digest is an integrity check:
it detects accidental corruption or edits; it does not prove who wrote the file.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .decisions import check_event as check_decision_event
from .export import canonical_stream, verify_export, verify_stream_digest
from .paths import resolve_db_path
from .schema import SYSTEM_CLAIM_KIND
from .store import (
    RESERVED_KIND_PREFIX,
    Event,
    Ledger,
    LedgerError,
    ValidationError,
    canonical_dumps,
    check_child_of_event,
    check_expected_system,
    check_system_claim,
    open_ledger,
)
from .store import _refuse_newer_schema_in_transaction

_INSERT = """INSERT INTO events (
    id, kind, payload_version, payload, item_id, idempotency_key,
    actor, harness, model, session, channel, provenance_class,
    recorded_at, prev_hash, hash
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


@dataclass(frozen=True)
class RestoreResult:
    path: Path
    events: int
    stream: bytes


def replay_events(lg: Ledger, events: list[Event]) -> None:
    """INSERT non-reserved exported rows as-is. Does not recompute hashes."""
    reserved = next((event for event in events if event.kind.startswith(RESERVED_KIND_PREFIX)), None)
    if reserved is not None:
        raise LedgerError(f"replay refuses reserved event: {reserved.kind}")
    _replay_events(lg, events)


def _replay_events(lg: Ledger, events: list[Event]) -> None:
    """INSERT rows in id order, each first passing the decision, claim and
    child-of guards.

    A valid hash chain proves the export was not edited after it was written,
    not that its decisions, its system claim or its anchors were ever
    allowed. The guards
    see only the rows replayed before them, as the original append did; one
    refusal rolls back the whole replay.
    """
    conn = lg._conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        _refuse_newer_schema_in_transaction(conn)
        for event in events:
            try:
                check_decision_event(conn, event.kind, event.payload, event.item_id)
            except ValidationError as exc:
                raise LedgerError(
                    f"restore refuses event {event.id} ({event.kind}): {exc.reason}"
                ) from exc
            try:
                check_system_claim(conn, event.kind, event.payload,
                                   f"{lg.path}, restored event {event.id}")
            except ValidationError as exc:
                raise LedgerError(
                    f"restore refuses event {event.id} ({event.kind}): {exc.reason}"
                ) from exc
            try:
                check_child_of_event(conn, event.kind, event.payload, event.item_id)
            except ValidationError as exc:
                raise LedgerError(
                    f"restore refuses event {event.id} ({event.kind}): {exc.reason}"
                ) from exc
            conn.execute(
                _INSERT,
                (
                    event.id,
                    event.kind,
                    event.payload_version,
                    canonical_dumps(event.payload),
                    event.item_id,
                    event.idempotency_key,
                    event.actor,
                    event.harness,
                    event.model,
                    event.session,
                    event.channel,
                    event.provenance_class,
                    event.recorded_at,
                    event.prev_hash,
                    event.hash,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _dest_files(dest_path: Path) -> list[Path]:
    """The destination DB and its WAL/SHM sidecars."""
    return [
        dest_path,
        dest_path.with_name(dest_path.name + "-wal"),
        dest_path.with_name(dest_path.name + "-shm"),
    ]


def restore_export(
    export_dir: Path | str,
    dest: Path | str,
    *,
    system: str | None,
    accept_loop_events: bool = False,
    on_accept_loop_events: Callable[[int, tuple[str, ...]], None] | None = None,
) -> RestoreResult:
    """Verify the export, replay into a new database, and verify the chain.

    Every restore is for one named ``system``; a restore with no system is
    refused before the export is read, so the target check is never
    skipped. An export another system claimed is refused with
    ``SharedDatabaseError`` before any file is written. An unclaimed export,
    such as one from before claims existed, is claimed for ``system`` after
    the replay, as ``Ledger.claim_system`` claims an unclaimed database; the
    restored chain is then verified against ``system``.
    """
    if system is None:
        raise ValidationError("restore-needs-system")
    export_path = Path(export_dir)
    events = verify_export(export_path)
    export_system = next(
        (event.payload["system"] for event in events if event.kind == SYSTEM_CLAIM_KIND),
        None,
    )
    if export_system is not None:
        check_expected_system(export_system, system, f"restore of {export_path}")
    reserved_events = [
        event for event in events if event.kind.startswith(RESERVED_KIND_PREFIX)
    ]
    export_digest: str | None = None
    if reserved_events:
        if not accept_loop_events:
            raise LedgerError(
                "restore contains loop-owned events; pass --accept-loop-events to restore them"
            )
        export_digest = verify_stream_digest(export_path, events)
    dest_path = resolve_db_path(dest)
    if dest_path.exists():
        raise LedgerError(f"restore target already exists: {dest_path}")
    stale = {p for p in _dest_files(dest_path) if p.exists()}
    lg = open_ledger(dest_path, create=True)
    try:
        if reserved_events:
            if on_accept_loop_events is not None:
                on_accept_loop_events(
                    len(reserved_events),
                    tuple(sorted({event.kind for event in reserved_events})),
                )
            _replay_events(lg, events)
        else:
            replay_events(lg, events)
        if export_digest is not None:
            lg.append(
                kind="session.opened",
                payload={
                    "purpose": "restore loop-managed export",
                    "source": f"sha256:{export_digest}",
                },
                idempotency_key=f"restore:loop-events:{export_digest}",
                provenance_class="restored-export",
            )
        if export_system is None:
            lg.claim_system(system)
        lg.verify(system=system)
        restored = lg.events()
    except BaseException:
        lg.close()
        for path in _dest_files(dest_path):
            if path not in stale:
                path.unlink(missing_ok=True)
        raise
    finally:
        lg.close()
    return RestoreResult(
        path=dest_path,
        events=len(restored),
        stream=canonical_stream(restored),
    )
