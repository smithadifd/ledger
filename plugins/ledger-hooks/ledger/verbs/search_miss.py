"""Log a search that missed: one line appended to the misses file.

The misses file (the database's file name plus ``.search-misses.jsonl``) sits
beside the ledger database and belongs to that database alone. The verb checks its three options against a fixed grammar (``ledger.search_misses``)
and then appends one line; it writes no event, builds no index and changes no
other file. A refusal writes nothing. It writes no event, so like ``export``
and ``backup`` (which write files, not events) it is not a ``writes`` verb and
needs no ``LEDGER_HARNESS``.
"""
from __future__ import annotations

import argparse

from ledger.card import _one_line
from ledger.paths import resolve_db_path
from ledger.search_misses import (
    MAX_EXPECTED_CHARS,
    MAX_NOTE_CHARS,
    MAX_QUERY_CHARS,
    MissError,
    append_miss,
)
from ledger.verbs import EXIT_NOT_FOUND, EXIT_REFUSED, CliError, Context, Result, UsageError, Verb

EPILOG = """\
Run it when a search failed to surface something you later found another way.
--expected is event:N (the document built from event N) or an item id (that
item's own document); anything else is refused and nothing is written.
`ledger search-audit` replays the logged misses against the current index.

examples:
  ledger search-miss --query "harness map" --expected event:412
  ledger search-miss --query "release.notes" --expected parent-item --note "found by item id"
"""


def add_arguments(parser) -> None:
    parser.epilog = EPILOG
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument(
        "--query",
        action="append",
        metavar="Q",
        help=f"the words that missed, up to {MAX_QUERY_CHARS} characters",
    )
    parser.add_argument(
        "--expected",
        action="append",
        metavar="EVENT_OR_ITEM",
        help=f"event:N or an item id, up to {MAX_EXPECTED_CHARS} characters, no whitespace",
    )
    parser.add_argument(
        "--note",
        action="append",
        metavar="T",
        help=f"optional, up to {MAX_NOTE_CHARS} characters",
    )


def _one(values: list[str] | None, flag: str, *, required: bool) -> str | None:
    """The single value ``flag`` was given, or a usage error naming the form."""
    if not values:
        if required:
            raise UsageError(f"missing-option: {flag} is required")
        return None
    if len(values) != 1:
        raise UsageError(f"repeated-option: {flag} is given once")
    return values[0]


def run(args, ctx: Context) -> Result:
    query = _one(args.query, "--query", required=True)
    expected = _one(args.expected, "--expected", required=True)
    note = _one(args.note, "--note", required=False)
    resolved = resolve_db_path(ctx.db)
    if not resolved.exists():
        raise CliError(
            f"database does not exist: {resolved}", code="missing_db", exit_code=EXIT_NOT_FOUND
        )
    try:
        path, entry = append_miss(ctx.db, query, expected, note)
    except MissError as exc:
        if exc.reason.startswith("misses-file-"):
            raise CliError(str(exc), code=exc.reason, exit_code=EXIT_REFUSED) from exc
        raise UsageError(str(exc)) from exc
    return Result(
        data={"path": str(path), "entry": entry},
        text=f"logged\t{_one_line(entry['expected'])}\t{_one_line(entry['query'])}",
    )


VERB = Verb(
    name="search-miss",
    help="log a search that missed: one appended line, nothing else",
    add_arguments=add_arguments,
    run=run,
)
