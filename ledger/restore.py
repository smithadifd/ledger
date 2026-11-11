"""Replay an export directory into a fresh ledger database.

Restore inserts the original ``id``, ``hash``, ``prev_hash`` and
``recorded_at`` values. It does not re-hash and does not drop the
append-only UPDATE/DELETE triggers. The replayed chain must then pass
``verify``. For loop-owned exports, the stream digest is an integrity check:
it detects accidental corruption or edits; it does not prove who wrote the file.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .credscan import scan as scan_credentials
from .credscan import shape_names
from .decisions import check_event as check_decision_event
from .export import canonical_stream, verify_export, verify_stream_digest
from .paths import resolve_db_path
from .schema import (
    DOC_CREDENTIAL_OVERRIDE,
    DOC_INLINE_LIMIT,
    DOC_KIND,
    SYSTEM_CLAIM_KIND,
    validate_payload,
)
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


DOC_DIGEST_MISMATCH = "restore_doc_digest_mismatch"
DOC_ITEM_MISSING = "restore_doc_item_missing"
DOC_CREDENTIAL = "restore_doc_credential"


def inline_document_bytes(payload: Any) -> bytes:
    """The bytes of an inline document's ``content_b64``.

    Raises ``ValueError`` when it is missing, not a string, not base64, or
    not the canonical encoding of its bytes (so one document has one stored
    form).
    """
    encoded = payload.get("content_b64") if isinstance(payload, dict) else None
    if type(encoded) is not str:
        raise ValueError("content_b64 is not a string")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"content_b64 is not base64: {exc}") from None
    if base64.b64encode(data).decode("ascii") != encoded:
        raise ValueError("content_b64 is not the canonical base64 of its bytes")
    return data


def check_document_event(event: Event) -> None:
    """Refuse a ``doc.attached`` event that would enter the store with a
    digest the ledger did not compute, or holding an unmarked credential
    shape.

    The payload must pass its schema, which takes exactly one of the inline
    bytes (``content_b64``) and a stored path (``path``), refusing both and
    neither (``exactly-one-of:content_b64,path``), refuses a stored path
    outside its grammar (``bad-doc-path:path``: absolute, empty, a trailing
    or doubled ``/``, a ``.`` or ``..`` component, a control character, or
    over its byte limits), a ``size`` over ``DOC_PATH_LIMIT``, and a
    ``credential_override`` other than true.

    A by-path document holds no bytes, so the schema is its whole check
    here: nothing is decoded, recomputed or scanned, and no file is opened.
    Its stored sha256 is the export's claim until ``doc verify`` or
    ``doc show --content`` reads the file under the restoring database's own
    document root and hashes it.

    An inline document's bytes are decoded and their ``size`` and ``sha256``
    recomputed; a malformed encoding, bytes over ``DOC_INLINE_LIMIT``, or a
    mismatch with the stored values refuses with
    ``restore_doc_digest_mismatch`` naming the event. The bytes are then
    scanned for credential shapes (``ledger.credscan``): a hit on an event
    without ``credential_override: true`` refuses with
    ``restore_doc_credential`` naming the event and the shapes, never the
    matched bytes. Other kinds pass.
    """
    if event.kind != DOC_KIND:
        return
    try:
        validate_payload(event.kind, event.payload, event.payload_version)
    except ValidationError as exc:
        raise LedgerError(
            f"restore refuses event {event.id} ({event.kind}): {exc.reason}"
        ) from exc
    if "path" in event.payload:
        return
    try:
        data = inline_document_bytes(event.payload)
    except ValueError as exc:
        problem = str(exc)
    else:
        problem = None
        if len(data) > DOC_INLINE_LIMIT:
            problem = f"{len(data)} inline bytes, over the {DOC_INLINE_LIMIT}-byte limit"
        elif len(data) != event.payload["size"]:
            problem = f"size {event.payload['size']} but the bytes are {len(data)}"
        elif hashlib.sha256(data).hexdigest() != event.payload["sha256"]:
            problem = "sha256 does not match the bytes"
    if problem is not None:
        raise LedgerError(
            f"restore refuses event {event.id} ({event.kind}): "
            f"{DOC_DIGEST_MISMATCH}: {problem}"
        )
    if event.payload.get(DOC_CREDENTIAL_OVERRIDE) is True:
        return
    hits = scan_credentials(data)
    if hits:
        raise LedgerError(
            f"restore refuses event {event.id} ({event.kind}): "
            f"{DOC_CREDENTIAL}: its bytes hold a credential shape "
            f"({shape_names(hits)}) and it carries no credential_override"
        )


def check_document_item(conn: sqlite3.Connection, event: Event) -> None:
    """Refuse a ``doc.attached`` event whose item is not an item yet.

    The check reads the items the replay transaction holds so far
    (``v_items``), as the decision and child-of guards do: an item already
    in the destination, or one whose ``item.created`` was replayed earlier
    in the same batch, counts. Otherwise it refuses with
    ``restore_doc_item_missing`` naming the event and the item. Other kinds
    pass.
    """
    if event.kind != DOC_KIND:
        return
    if event.item_id is None or conn.execute(
        "SELECT 1 FROM v_items WHERE item_id = ?", (event.item_id,)
    ).fetchone() is None:
        raise LedgerError(
            f"restore refuses event {event.id} ({event.kind}): "
            f"{DOC_ITEM_MISSING}: item not found: {event.item_id}"
        )


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

    Before anything is inserted, every ``doc.attached`` event in the batch
    passes its schema, and an inline one has its digest recomputed and its
    bytes scanned for credential shapes (``check_document_event``). This is
    the one path ``restore_export`` and ``replay_events`` share, so neither
    can insert an inline document whose sha256 the ledger did not compute,
    one holding a credential shape without the override marker, or a by-path
    document whose stored path breaks its grammar. Neither opens a document
    file: a by-path document is inserted as a reference. Inside the
    transaction, each ``doc.attached`` must also name an item the rows
    replayed before it hold (``check_document_item``).
    """
    for event in events:
        check_document_event(event)
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
            check_document_item(conn, event)
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
