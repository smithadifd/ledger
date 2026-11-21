"""Replay the logged search misses against the current search index.

Prints the rank each miss (``ledger search-miss``) gets now. A miss that ranks
past ten, or not at all, is a candidate golden case. The verb only reads: it
never refreshes the index, so ``search-audit`` on an index that is behind,
stale or missing is unverifiable (exit 3, no rank printed) until ``ledger
index`` brings it up to date. A misses file that cannot be read, or has a line
off its grammar, is unverifiable the same way.
"""
from __future__ import annotations

import sqlite3

from ledger.card import _one_line
from ledger.paths import resolve_db_path
from ledger.search import IndexNotCurrent, SearchError
from ledger.search_misses import DEFAULT_RANK, MISSES_NAME, MissError, misses_path, read_misses, replay
from ledger.store import LedgerError
from ledger.verbs import EXIT_NOT_FOUND, EXIT_REFUSED, CliError, Context, Result, Verb


def add_arguments(parser) -> None:
    return


def _unverifiable(reason: str) -> CliError:
    return CliError(f"search-audit is unverifiable: {reason}", code="unverifiable", exit_code=EXIT_REFUSED)


def _line(row: dict) -> str:
    rank = row["rank"] if row["rank"] is not None else "none"
    text = (
        f"{'candidate' if row['candidate'] else 'ok'}\trank={rank}\t"
        f"{_one_line(row['expected'])}\t{_one_line(row['query'])}"
    )
    if "note" in row:
        text += f"\n\tnote: {_one_line(row['note'])}"
    return text


def run(args, ctx: Context) -> Result:
    resolved = resolve_db_path(ctx.db)
    if not resolved.exists():
        raise CliError(
            f"database does not exist: {resolved}", code="missing_db", exit_code=EXIT_NOT_FOUND
        )
    try:
        misses = read_misses(resolved)
    except MissError as exc:
        raise _unverifiable(str(exc)) from exc
    data = {"path": str(misses_path(resolved)), "default_rank": DEFAULT_RANK, "misses": []}
    if not misses:
        return Result(data=data, text=f"no logged misses ({MISSES_NAME})")
    try:
        rows = replay(resolved, misses, ctx.search_fallback_root)
    except IndexNotCurrent as exc:
        raise _unverifiable(f"{exc}; run ledger index, then search-audit again") from exc
    except (SearchError, LedgerError, sqlite3.Error, OSError) as exc:
        raise _unverifiable(f"cannot read the search index: {exc}") from exc
    data["misses"] = rows
    candidates = sum(1 for row in rows if row["candidate"])
    summary = (
        f"{len(rows)} logged {'miss' if len(rows) == 1 else 'misses'} replayed, "
        f"{candidates} candidate golden {'case' if candidates == 1 else 'cases'} "
        f"(rank past {DEFAULT_RANK} or none)"
    )
    return Result(data=data, text="\n".join([*(_line(row) for row in rows), summary]))


VERB = Verb(
    name="search-audit",
    help="replay the logged search misses and print the rank each gets now",
    add_arguments=add_arguments,
    run=run,
)
