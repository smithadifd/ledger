"""Build the derived search index (search.db) beside the ledger database.

A current index is left alone. A behind index (only new events since it was
built) gets those events appended; anything else, or ``--rebuild``, rebuilds
it from scratch.
"""
from __future__ import annotations

from ledger.paths import resolve_db_path
from ledger.search import (
    SearchError,
    build_index,
    config_file,
    index_status,
    refresh_index,
    refuse_inside_git,
)
from ledger.verbs import EXIT_REFUSED, CliError, Context, Result, Verb


def add_arguments(parser) -> None:
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="rebuild even when the index is current",
    )


def run(args, ctx: Context) -> Result:
    resolved = resolve_db_path(ctx.db)
    try:
        refuse_inside_git(resolved)  # before the fast path, not only on a rebuild
        if not args.rebuild:
            status = index_status(resolved, ctx.search_fallback_root)
            if status["status"] == "current":
                data = {"path": status["path"], "rebuilt": False, "status": "current"}
                return Result(data=data, text=f"current {status['path']}")
            summary = refresh_index(resolved, ctx.search_fallback_root)
        else:
            summary = {**build_index(resolved, ctx.search_fallback_root), "refresh": "rebuild"}
    except SearchError as exc:
        raise CliError(str(exc), code="search", exit_code=EXIT_REFUSED) from exc
    data = {**summary, "rebuilt": True, "status": "current"}
    counts = " ".join(f"{kind}={count}" for kind, count in summary["by_type"].items())
    text = f"indexed {summary['docs']} docs ({counts or 'none'}) into {summary['path']}"
    if summary["refresh"] == "append":
        count = summary["new_events"]
        text += f", appending {count} new {'event' if count == 1 else 'events'}"
    if summary["note"]:
        # Not a failure: a fresh install indexes the ledger and nothing else.
        text += (
            f"\nonly the ledger is indexed ({summary['note']}); to index markdown too, "
            f"set search_roots to a list of absolute paths in {config_file(resolved)}"
        )
    return Result(data=data, text=text)


VERB = Verb(
    name="index",
    help="build search.db beside the ledger (derived, never exported)",
    add_arguments=add_arguments,
    run=run,
)
