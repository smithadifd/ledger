"""Print the resolved DB path and schema version; non-zero if unusable.

It also reports whether the derived search index is missing, stale or
current. That report never changes the exit code.

It also lists claims whose lease has expired, with holder and age, so a dead
seat's claim is noticed. That list never changes the exit code either.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ledger import lease, search
from ledger.paths import resolve_db_path
from ledger.store import _recorded_versions, _refuse_unknown_versions, load_migrations
from ledger.verbs import EXIT_NOT_FOUND, EXIT_REFUSED, CliError, Context, Result, Verb


def add_arguments(parser) -> None:
    return


def _schema_version(path: Path) -> int:
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
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
        }


def _expired_claims(resolved: Path) -> dict:
    """Expired claims; any failure reads as ``unknown``, never an error."""
    try:
        uri = f"file:{resolved}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            return {"status": "ok", "items": lease.expired_leases(conn)}
        finally:
            conn.close()
    except Exception as exc:  # an unmigrated or unreadable file is not a doctor failure
        return {"status": "unknown", "items": [], "reason": f"{type(exc).__name__}: {exc}"}


def _expired_line(c: dict) -> str:
    if c.get("unreadable_stamp"):
        return f"expired claim: {c['item_id']}\tholder={c['holder']}\tstamp=unreadable\n"
    return (
        f"expired claim: {c['item_id']}\tholder={c['holder']}"
        f"\tage={c['age_seconds']}s\texpired_for={c['expired_for_seconds']}s\n"
    )


def run(args, ctx: Context) -> Result:
    resolved = resolve_db_path(ctx.db)
    if not resolved.exists():
        raise CliError(
            f"database does not exist: {resolved}",
            code="missing_db",
            exit_code=EXIT_NOT_FOUND,
        )
    version = _schema_version(resolved)
    found = _search_status(resolved, ctx)
    claims = _expired_claims(resolved)
    data = {
        "path": str(resolved),
        "schema_version": version,
        "search": found,
        "expired_claims": claims,
    }
    hint = "" if found["status"] == "current" else " (ledger index rebuilds it)"
    return Result(
        data=data,
        text=(
            f"path: {resolved}\nschema_version: {version}\n"
            f"search: {found['status']}{hint}\n"
        )
        + "".join(_expired_line(c) for c in claims["items"]),
    )


VERB = Verb(
    name="doctor",
    help="print the resolved DB path and schema version",
    add_arguments=add_arguments,
    run=run,
)
