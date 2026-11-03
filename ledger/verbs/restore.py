"""Replay an export directory into a fresh database."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ledger.restore import restore_export
from ledger.verbs import Context, Result, UsageError, Verb


def add_arguments(parser) -> None:
    parser.add_argument(
        "--dir",
        dest="export_dir",
        help="export directory (or LEDGER_EXPORT_DIR)",
    )
    parser.add_argument(
        "--system",
        help="the system (for example work or home) this restore is for; required",
    )
    parser.add_argument(
        "--accept-loop-events",
        action="store_true",
        help="explicitly allow loop-owned events after an export integrity check",
    )


def _announce_loop_events(count: int, kinds: tuple[str, ...]) -> None:
    print(
        f"restore: accepting {count} loop-reserved events "
        f"(kinds: {', '.join(kinds)}) because --accept-loop-events was given",
        file=sys.stderr,
    )


def run(args, ctx: Context) -> Result:
    raw = args.export_dir or os.environ.get("LEDGER_EXPORT_DIR")
    if not raw:
        raise UsageError("pass --dir or set LEDGER_EXPORT_DIR")
    # The restore is always for a named system, so an export another system
    # claimed is refused rather than restored unchecked.
    if not args.system:
        raise UsageError("pass --system")
    result = restore_export(
        Path(raw),
        ctx.db,
        system=args.system,
        accept_loop_events=args.accept_loop_events,
        on_accept_loop_events=_announce_loop_events,
    )
    return Result(
        data={"events": result.events, "path": str(result.path)},
        text=f"restored {result.events} events into {result.path}",
    )


VERB = Verb(
    name="restore",
    help="replay exported events into a fresh database",
    add_arguments=add_arguments,
    run=run,
)
