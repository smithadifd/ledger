"""Restore an export and compare its replay with the source event stream."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .backup import integrity_check
from .export import canonical_stream, verify_export
from .restore import restore_export
from .store import Event, open_ledger

# An unclaimed ledger (one from before system claims) restores under this
# name; the restore appends its claim after the replayed stream, and the
# throwaway database is discarded with the drill.
UNCLAIMED_DRILL_SYSTEM = "restore-drill"


@dataclass(frozen=True)
class RestoreDrillResult:
    events: int
    sha256: str


def restore_drill(
    source: list[Event],
    export_dir: Path,
    restored_db: Path,
    *,
    system: str | None,
) -> RestoreDrillResult:
    """Verify and restore ``export_dir``, then match it to ``source``.

    ``system`` is the live ledger's claimed system, or ``None`` when it has
    never been claimed.
    """
    exported = verify_export(export_dir, system=system)
    exported_count = len(exported)
    if exported_count == 0 and source:
        raise RuntimeError("restore drill: export is empty")
    if len(source) < exported_count:
        raise RuntimeError("restore drill: export count mismatch")
    source_prefix = source[:exported_count]
    source_digest = hashlib.sha256(canonical_stream(source_prefix)).hexdigest()
    export_digest = hashlib.sha256(canonical_stream(exported)).hexdigest()
    if source_digest != export_digest:
        raise RuntimeError("restore drill: event hash mismatch")

    reserved = any(event.kind.startswith("loop.") for event in source_prefix)
    restore_system = system if system is not None else UNCLAIMED_DRILL_SYSTEM
    restore_export(
        export_dir,
        restored_db,
        accept_loop_events=reserved,
        system=restore_system,
    )
    if integrity_check(restored_db) != "ok":
        raise RuntimeError("restore drill: integrity_check failed")

    replay = open_ledger(restored_db, create=False)
    try:
        replay.verify(system=restore_system)
        restored = replay.events()
    finally:
        replay.close()

    # Loop restores append one receipt after replaying the exported stream,
    # and an unclaimed export gets its claim appended the same way.
    expected_count = exported_count + int(reserved) + int(system is None)
    if len(restored) != expected_count:
        raise RuntimeError("restore drill: event-count mismatch")
    replay_digest = hashlib.sha256(
        canonical_stream(restored[:exported_count])
    ).hexdigest()
    if source_digest != replay_digest:
        raise RuntimeError("restore drill: event hash mismatch")
    return RestoreDrillResult(events=exported_count, sha256=source_digest)
