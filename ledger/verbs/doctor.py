"""Print the resolved DB path and schema version; non-zero if unusable.

It also reports whether the derived search index is missing, stale,
behind (only newer events) or current, with a severity (``ok``, ``info``, or
``warn`` when the index cannot be built). That report never changes the exit
code. The text line follows the severity: ``ok`` adds nothing; ``info`` says
the index rebuilds itself, which it does; ``warn`` names the reason and what
clears it, and never says the index rebuilds itself. A warn reason this file
does not know is printed as it is, with a pointer to ``ledger doctor --json``.

It also lists claims whose lease has expired, with holder and age, so a dead
seat's claim is noticed. That list never changes the exit code either.

It prints, and ``--json`` carries under ``stamps``, the four actor stamps (actor,
harness, model, session) a write from this environment would record. They come
from ``actor_kwargs(ctx)``, the function every write stamps its event with, so
doctor cannot drift from what a write records, and doctor reads no environment
variable itself. An unset stamp is the word ``missing``.

Stale claims are listed beside the expired claims: claims with no lease stamp whose claim
event is older than ``lease.STALE_CLAIM_SECONDS`` (7 days), each with a
release command doctor prints and never runs. A stale claim of an unfinished
session hand-off (``ledger.loop.session_hand_off``) gets no release command:
a generic ``state --status open`` would not move the loop's claim, and its
recovery is ``reclaim-handoff``, which needs a successor and evidence doctor
cannot know, so it names that verb and never prints a command. Before it reports the search
index, doctor refreshes it as ``search`` does; when that cannot write (an
unwritable index directory), it reports the state it reads and keeps its
exit code and JSON shape.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

from ledger import lease, loop, search
from ledger.card import _one_line
from ledger.dbfile import UnsafeDatabaseFile
from ledger.export import read_watermark, resolve_export_dir
from ledger.paths import resolve_db_path
from ledger.store import (
    LedgerError,
    _connect_readonly,
    _recorded_versions,
    _refuse_unknown_versions,
    load_migrations,
)
from ledger.verbs import (
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    Verb,
    actor_kwargs,
    invocation_prefix,
    quote_command_arg,
)


def add_arguments(parser) -> None:
    return


# The stamps a write records, in the order doctor prints them, and what an
# unset one reads as.
STAMP_NAMES = ("actor", "harness", "model", "session")
MISSING = "missing"


def _stamps(ctx: Context) -> dict[str, str]:
    """The stamps a write from this context would record; ``missing`` for an
    unset or empty one, never a guessed value."""
    stamped = actor_kwargs(ctx)
    return {name: stamped[name] or MISSING for name in STAMP_NAMES}


def _stamp_lines(stamps: dict[str, str]) -> str:
    """One ``name: value`` line per stamp. The value is escaped so a control
    character in it cannot end its line or forge another."""
    return "".join(f"{name}: {_one_line(value)}\n" for name, value in stamps.items())


def _schema_version(path: Path) -> int:
    try:
        conn = _connect_readonly(path)
    except sqlite3.Error as exc:
        raise CliError(
            f"unusable database: {exc}",
            code="unusable",
            exit_code=EXIT_REFUSED,
        ) from exc
    try:
        try:
            # One read transaction: the version check and the read it guards
            # see the same snapshot, so a newer migration committed between
            # them is never reported as this database's version.
            conn.execute("BEGIN")
            _refuse_unknown_versions(_recorded_versions(conn), load_migrations())
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        except sqlite3.Error as exc:
            raise CliError(
                f"unusable database: {exc}",
                code="unusable",
                exit_code=EXIT_REFUSED,
            ) from exc
        if row is None or row[0] is None:
            raise CliError(
                "unusable database: no schema_version",
                code="unusable",
                exit_code=EXIT_REFUSED,
            )
        return int(row[0])
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def _search_status(resolved: Path, ctx: Context) -> dict:
    """The search index's state; any failure reads as ``unknown``, never an error."""
    try:
        return search.index_status(resolved, ctx.search_fallback_root)
    except Exception as exc:  # doctor never fails on the derived index
        return {
            "path": str(resolved.parent / search.SEARCH_DB_NAME),
            "status": "unknown",
            "reasons": [f"{type(exc).__name__}: {exc}"],
            "severity": search.severity_for("unknown"),
        }


# The reasons ``search.index_status`` gives for a ``warn`` severity, matched by
# the fixed start ``ledger/search.py`` words them with, and what clears each.
_WARN_REMEDIES: tuple[tuple[str, str], ...] = (
    (
        f"refusing to use {search.SEARCH_DB_NAME} inside a git work tree: ",
        "move the ledger database to a directory outside the git work tree",
    ),
    (
        f"{search.SEARCH_DB_NAME} is a directory, not a regular file: ",
        "move that directory away from the index path",
    ),
    (
        "unreadable ledger config ",
        f"make {search.CONFIG_NAME} readable, valid JSON",
    ),
    (
        "ledger config ",
        f"make {search.CONFIG_NAME} a JSON object",
    ),
    (
        "search_roots must be a list of absolute paths",
        f"set search_roots in {search.CONFIG_NAME} to a list of absolute paths",
    ),
    (
        "search_roots entry is not absolute: ",
        f"make every search_roots entry in {search.CONFIG_NAME} an absolute path",
    ),
    (
        "cannot stat the search index ",
        "make the index path's directory readable",
    ),
    (
        "refusing to replace ",
        f"move or rename that file; {search.SEARCH_DB_NAME} beside a ledger is its index",
    ),
)

_REBUILD_HINT = " (ledger index rebuilds it)"


def _remedy(reason: str) -> str | None:
    for prefix, remedy in _WARN_REMEDIES:
        if reason.startswith(prefix):
            return remedy
    return None


def _warn_hint(found: dict) -> str:
    """Each known warn reason with its remedy; any other reason verbatim with a
    pointer to the JSON form. Never the rebuild claim."""
    reasons = [str(r) for r in found.get("reasons") or []]
    known = [(r, _remedy(r)) for r in reasons]
    parts = [
        f"ledger index refuses: {reason}; to clear it, {remedy}"
        for reason, remedy in known
        if remedy is not None
    ]
    unmatched = [reason for reason, remedy in known if remedy is None]
    # A stale state's reasons are information, not the cause of the warning;
    # an unknown state's reasons, or a warning no known reason explains, are
    # printed as they are.
    verbatim = unmatched if found.get("status") not in search.SEVERITIES or not parts else []
    if not parts and not verbatim:
        verbatim = ["no reason given"]
    if verbatim:
        parts.append(f"{'; '.join(verbatim)}; see ledger doctor --json")
    return f" (warn: {'; '.join(parts)})"


def _search_line(found: dict) -> str:
    """``ok``: the state alone. ``info``: the rebuild parenthetical, or the
    behind count. Anything else is a warning: its reason and remedy."""
    severity = found.get("severity")
    status = found.get("status")
    if severity == "ok":
        return f"{status}"
    if severity == "info":
        if status == "behind":
            count = found["new_events"]
            plural = "event" if count == 1 else "events"
            return (
                f"current to event {found['indexed_to_event']}, "
                f"{count} new {plural} will be indexed on the next search"
            )
        return f"{status}{_REBUILD_HINT}"
    return f"{status}{_warn_hint(found)}"


def _expired_claims(resolved: Path) -> dict:
    """Expired claims; any failure reads as ``unknown``, never an error."""
    try:
        conn = _connect_readonly(resolved)
        conn.row_factory = sqlite3.Row
        try:
            return {"status": "ok", "items": lease.expired_leases(conn)}
        finally:
            conn.close()
    except UnsafeDatabaseFile:
        raise
    except Exception as exc:  # an unmigrated or unreadable file is not a doctor failure
        return {"status": "unknown", "items": [], "reason": f"{type(exc).__name__}: {exc}"}


def _expired_line(c: dict) -> str:
    if c.get("unreadable_stamp"):
        return f"expired claim: {c['item_id']}\tholder={c['holder']}\tstamp=unreadable\n"
    return (
        f"expired claim: {c['item_id']}\tholder={c['holder']}"
        f"\tage={c['age_seconds']}s\texpired_for={c['expired_for_seconds']}s\n"
    )


def _refresh_search(resolved: Path, ctx: Context) -> None:
    """Bring the search index up to date before it is reported, as ``search`` does.

    Only when the index is not current, a rebuild would not refuse (the
    severity is not ``warn``), and the index path is absent or a regular file;
    anything else is reported as found. A file at the index path that the
    index writer will not replace (a ledger, such as one named ``search.db``,
    or anything else that is not a search index) reads as ``warn``, so it is
    reported, never refreshed; the writer refuses it as well. A refresh that
    cannot write (an unwritable index directory raises ``OSError``) is not a
    doctor failure: the exit code and the JSON shape stay as they are, and
    doctor reports the state it then reads.
    """
    try:
        found = search.index_status(resolved, ctx.search_fallback_root)
    except Exception:  # _search_status reports it as unknown
        return
    if found.get("status") == "current" or found.get("severity") == "warn":
        return
    index = search.search_db_path(resolved)
    try:
        present = os.lstat(index)
    except FileNotFoundError:
        present = None
    except OSError:
        return
    if present is not None and not stat.S_ISREG(present.st_mode):
        return
    try:
        search.refresh_index(resolved, ctx.search_fallback_root)
    except OSError:
        return
    except (sqlite3.Error, search.SearchError, LedgerError):
        # LedgerError: a newer schema committed after doctor's version check,
        # or a database that still needs migrations and has no index yet (the
        # first doctor after an upgrade); doctor reports the state it reads.
        return


def _release_command(prefix: str, item_id: str, revision: int) -> str:
    """The command that releases a stale claim; doctor prints it, never runs it.

    ``prefix`` always names the database doctor opened (``--db``), so the
    command never falls back to another ``LEDGER_DB`` and releases an item
    in an unrelated database. ``--expect-revision`` makes it refuse once the
    item has moved on since doctor read it.
    """
    return (
        f"{prefix} state --status open --expect-revision {revision} -- "
        f"{quote_command_arg(item_id)}"
    )


def _stale_claims(resolved: Path, ctx: Context, expired: dict) -> dict:
    """Stamp-less claims past ``lease.STALE_CLAIM_SECONDS``, disjoint from
    ``expired``, each with its release command; any failure reads as
    ``unknown``, never an error.

    A session hand-off's claim (``loop.session_hand_off`` names an
    unfinished session-mode operation on the item) gets ``release_command``
    ``None`` and ``session_hand_off`` set to that operation: its recovery is
    ``reclaim-handoff``, never a generic release. Every other row has
    ``session_hand_off`` ``None``."""
    try:
        conn = _connect_readonly(resolved)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            listed = {c["item_id"] for c in expired.get("items") or []}
            prefix = invocation_prefix(ctx.program, str(resolved), True)
            items = []
            for row in lease.stale_claims(conn):
                if row["item_id"] in listed:
                    continue
                hand_off = loop.session_hand_off(conn, row["item_id"])
                row["session_hand_off"] = hand_off
                if hand_off is not None:
                    row["release_command"] = None
                    items.append(row)
                    continue
                revision = conn.execute(
                    "SELECT MAX(id) FROM events WHERE item_id = ?", (row["item_id"],)
                ).fetchone()[0]
                row["release_command"] = _release_command(prefix, row["item_id"], revision)
                items.append(row)
            return {"status": "ok", "items": items}
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()
    except UnsafeDatabaseFile:
        raise
    except Exception as exc:  # an unmigrated or unreadable file is not a doctor failure
        return {"status": "unknown", "items": [], "reason": f"{type(exc).__name__}: {exc}"}


def _stale_line(c: dict) -> str:
    head = f"stale claim: {c['item_id']}\tholder={c['holder']}\tage={c['age_seconds']}s"
    hand_off = c.get("session_hand_off")
    if hand_off is not None:
        # No command: the successor and the evidence are the caller's to name.
        return (
            f"{head}\tsession hand-off: intent event {hand_off['intent_event_id']}; "
            "recover it with reclaim-handoff --declare-seat-lost, naming the successor "
            "and the evidence (ledger reclaim-handoff --help)\n"
        )
    return f"{head}\trelease: {c['release_command']}\n"


def _counts(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, n in conn.execute(sql):
        if key is None:
            continue
        out[str(key)] = int(n)
    return out


def _export_part(conn: sqlite3.Connection, export_dir: str | Path) -> dict:
    try:
        resolved = resolve_export_dir(export_dir)
        watermark = read_watermark(resolved)
        behind = conn.execute(
            "SELECT COUNT(*) FROM events WHERE id > ?", (watermark,)
        ).fetchone()[0]
        return {"watermark_event_id": watermark, "behind": int(behind)}
    except Exception as exc:
        return {"status": "unknown", "reason": f"{type(exc).__name__}: {exc}"}


def _summary_snapshot(resolved: Path) -> dict:
    conn = _connect_readonly(resolved)
    try:
        conn.execute("BEGIN")
        events_total, last_event_at = conn.execute(
            "SELECT COUNT(*), MAX(recorded_at) FROM events"
        ).fetchone()
        data = {
            "items_by_type": _counts(
                conn, "SELECT type, COUNT(*) FROM v_items GROUP BY type"
            ),
            "items_by_status": _counts(
                conn, "SELECT status, COUNT(*) FROM v_items GROUP BY status"
            ),
            "events_total": int(events_total),
            "last_event_at": last_event_at,
            "open_questions": int(
                conn.execute("SELECT COUNT(*) FROM v_open_decisions").fetchone()[0]
            ),
            "leases": int(conn.execute("SELECT COUNT(*) FROM v_leases").fetchone()[0]),
        }
        raw = os.environ.get("LEDGER_EXPORT_DIR")
        if raw:
            data["export"] = _export_part(conn, raw)
        return data
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def _summary(resolved: Path) -> dict:
    """Store counts; any failure reads as ``unknown``, never an error."""
    try:
        return _summary_snapshot(resolved)
    except UnsafeDatabaseFile:
        raise
    except Exception as exc:  # doctor never fails on the summary
        return {"status": "unknown", "reason": f"{type(exc).__name__}: {exc}"}


def run(args, ctx: Context) -> Result:
    resolved = resolve_db_path(ctx.db)
    if not resolved.exists():
        raise CliError(
            f"database does not exist: {resolved}",
            code="missing_db",
            exit_code=EXIT_NOT_FOUND,
        )
    version = _schema_version(resolved)
    _refresh_search(resolved, ctx)
    found = _search_status(resolved, ctx)
    claims = _expired_claims(resolved)
    stamps = _stamps(ctx)
    stale = _stale_claims(resolved, ctx, claims)
    data = {
        "path": str(resolved),
        "schema_version": version,
        "search": found,
        "stamps": stamps,
        "expired_claims": claims,
        "stale_claims": stale,
        "summary": _summary(resolved),
    }
    return Result(
        data=data,
        text=(
            f"path: {resolved}\nschema_version: {version}\n"
            f"search: {_search_line(found)}\n"
        )
        + _stamp_lines(stamps)
        + "".join(_expired_line(c) for c in claims["items"])
        + "".join(_stale_line(c) for c in stale["items"]),
    )


VERB = Verb(
    name="doctor",
    help="print the resolved DB path and schema version",
    add_arguments=add_arguments,
    run=run,
)
