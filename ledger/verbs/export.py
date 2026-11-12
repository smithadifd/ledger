"""Export events after the watermark; optional commit, push, or lag check."""
from __future__ import annotations

import os
from pathlib import Path

from ledger.export import (
    DEFAULT_MAX_EVENTS,
    DEFAULT_MAX_HOURS,
    EXPORT_DOC_CREDENTIAL,
    ExportDocCredential,
    ExportRefusal,
    LagExceeded,
    check_lag,
    commit_export,
    escape_name,
    export_events,
    push_export,
)
from ledger.verbs import (
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    open_db,
)


def add_arguments(parser) -> None:
    parser.add_argument(
        "--dir",
        dest="export_dir",
        help="export directory (or LEDGER_EXPORT_DIR)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "scan the exact tree, then make one git commit in the export directory; "
            "never pushes"
        ),
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="scan the whole history, then push the branch's commit to its push remote",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when lag exceeds --max-events or --max-hours",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=DEFAULT_MAX_EVENTS,
        help=f"lag limit in events for --check (default {DEFAULT_MAX_EVENTS})",
    )
    parser.add_argument(
        "--max-hours",
        type=float,
        default=DEFAULT_MAX_HOURS,
        help=f"lag limit in hours for --check (default {DEFAULT_MAX_HOURS})",
    )


def _export_dir(args) -> Path:
    raw = args.export_dir or os.environ.get("LEDGER_EXPORT_DIR")
    if not raw:
        raise UsageError("pass --dir or set LEDGER_EXPORT_DIR")
    return Path(raw)


def run(args, ctx: Context) -> Result:
    export_dir = _export_dir(args)
    if args.check and (args.commit or args.push):
        raise UsageError("--check cannot be combined with --commit or --push")
    if args.commit and args.push:
        raise UsageError("export --commit never pushes; run --push separately")
    if args.push:
        try:
            pushed = push_export(export_dir)
        except ExportRefusal as exc:
            raise CliError(str(exc), code=exc.code, exit_code=EXIT_REFUSED) from exc
        return Result(
            data={
                "branch": pushed.branch,
                "pushed": True,
                "remote": pushed.remote,
                "sha": pushed.sha,
            },
            text=(
                f"pushed {pushed.sha} to refs/heads/{escape_name(pushed.branch)}"
                + (f" at {escape_name(pushed.remote)}" if pushed.remote is not None else "")
            ),
        )
    lg = open_db(ctx, write=False)
    try:
        if args.check:
            try:
                lag = check_lag(
                    lg,
                    export_dir,
                    max_events=args.max_events,
                    max_hours=args.max_hours,
                )
            except LagExceeded as exc:
                raise CliError(
                    str(exc),
                    code="lag",
                    exit_code=EXIT_REFUSED,
                ) from exc
            data = {
                "events": lag.events,
                "hours": lag.hours,
                "oldest_recorded_at": lag.oldest_recorded_at,
                "watermark": lag.watermark,
            }
            return Result(
                data=data,
                text=(
                    f"ok lag events={lag.events} hours={lag.hours:.1f} "
                    f"watermark={lag.watermark}"
                ),
            )
        try:
            result = export_events(lg, export_dir)
        except ExportDocCredential as exc:
            # Refused before any file is written: no watermark move, and
            # --commit does not run.
            raise CliError(
                str(exc), code=EXPORT_DOC_CREDENTIAL, exit_code=EXIT_REFUSED
            ) from exc
    finally:
        lg.close()
    commit_sha = None
    index_refreshed = False
    if args.commit:
        try:
            committed = commit_export(export_dir)
        except ExportRefusal as exc:
            raise CliError(str(exc), code=exc.code, exit_code=EXIT_REFUSED) from exc
        commit_sha = committed.sha
        index_refreshed = committed.index_refreshed
    data = {
        "commit": commit_sha,
        "exported": result.exported,
        "watermark": result.watermark,
        "watermark_before": result.watermark_before,
    }
    text = f"exported {result.exported} watermark={result.watermark}"
    if args.commit:
        data["index_refreshed"] = index_refreshed
        text += f" commit={commit_sha or '-'}"
        if commit_sha is not None and not index_refreshed:
            text += (
                " (warning: the git index was not refreshed; run git read-tree HEAD "
                "in the export directory)"
            )
    return Result(data=data, text=text)


VERB = Verb(
    name="export",
    help="write events after the watermark as canonical JSON files",
    add_arguments=add_arguments,
    run=run,
)
