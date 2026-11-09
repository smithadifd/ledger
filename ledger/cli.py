"""Generic ledger CLI for user-authored events and projections.

User errors print one line, or a stable ``{ok, data, error}`` envelope with
``--json``, and never a stack trace. Exit codes: 0 ok, 1 not found/empty, 2
usage, 3 refused (validation, synced path, shared database, missing
LEDGER_HARNESS, capacity), 4 integrity failure from verify, 5 stale
revision (``--expect-revision`` no longer matches the item's newest event).
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping

from ledger.dbfile import UnsafeDatabaseFile
from ledger.paths import SharedDatabaseError
from ledger.schema import ValidationError
from ledger.store import (
    ChainBroken,
    ForestRefusal,
    IdempotencyConflict,
    LedgerError,
    StaleRevision,
    SyncPathError,
)
from ledger.verbs import (
    EXIT_INTEGRITY,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_STALE,
    EXIT_USAGE,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    compose_actor,
    discover_verbs,
)


class LedgerParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value


def _package_version() -> str:
    try:
        return importlib.metadata.version("ledger")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _program_name() -> str:
    configured = _env("LEDGER_PROG")
    if configured and not any(char in configured for char in "\r\n"):
        return configured
    try:
        started = Path(sys.argv[0])
        if started.name == "__main__.py":
            if started.parent.name == "ledger":
                return "python3 -m ledger"
            return "ledger"
        return shlex.quote(started.name or "ledger")
    except (OSError, TypeError, ValueError):
        return "ledger"


def _db_was_explicit(argv: list[str]) -> bool:
    return any(arg == "--db" or arg.startswith("--db=") for arg in argv)


def build_parser(table: dict[str, Verb]) -> LedgerParser:
    parser = LedgerParser(
        prog="ledger",
        description=(
            "Append-only item ledger. Generic writes require LEDGER_HARNESS; "
            "loop-reserved events use the internal library writer. Actor fields come from "
            "LEDGER_OPERATOR, LEDGER_AGENT, LEDGER_HARNESS, LEDGER_MODEL, LEDGER_SESSION; "
            "the actor is stamped as <operator>/<agent> when both are set. "
            "Global flags --json and --db may appear before or after the verb."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ledger {_package_version()}",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a stable {ok, data, error} envelope",
    )
    parser.add_argument("--db", help="database path (overrides LEDGER_DB)")
    sub = parser.add_subparsers(dest="verb", required=True)
    for name, verb in sorted(table.items()):
        subparser = sub.add_parser(name, help=verb.help)
        subparser.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="print a stable {ok, data, error} envelope",
        )
        subparser.add_argument(
            "--db",
            default=argparse.SUPPRESS,
            help="database path (overrides LEDGER_DB)",
        )
        verb.add_arguments(subparser)
        subparser.set_defaults(_verb=verb)
    return parser


def _fail(json_mode: bool, code: str, message: str, exit_code: int) -> int:
    if json_mode:
        print(
            json.dumps(
                {
                    "data": None,
                    "error": {"code": code, "message": message},
                    "ok": False,
                },
                sort_keys=True,
            )
        )
    else:
        print(message, file=sys.stderr)
    return exit_code


def _ok(json_mode: bool, result: Result) -> int:
    if json_mode:
        print(
            json.dumps(
                {"data": result.data, "error": None, "ok": True},
                sort_keys=True,
                default=str,
            )
        )
    elif result.text:
        sys.stdout.write(result.text if result.text.endswith("\n") else result.text + "\n")
    return result.exit_code


def _take_late_positionals(
    parser: argparse.ArgumentParser, args: argparse.Namespace, extras: list[str]
) -> argparse.Namespace:
    """Give leftover positionals to the verb's ``late_positional`` list.

    Older argparse (3.11) does not fill a positional that comes after an
    option (``link ITEM --rel pr 123``) and returns it as an extra; newer
    versions fill it, so there is nothing left. Anything else left over, an
    option or any extra for a verb that declares no such positional, is the
    usual usage error.
    """
    if not extras:
        return args
    verb = getattr(args, "_verb", None)
    dest = getattr(verb, "late_positional", None)
    if dest is None or any(extra.startswith("-") for extra in extras):
        parser.error("unrecognized arguments: " + " ".join(extras))
    setattr(args, dest, [*(getattr(args, dest, None) or []), *extras])
    return args


def _run(
    argv: list[str],
    json_mode: bool,
    roadmap_parser: Path | None,
    extra_verbs: Mapping[str, Verb] | None = None,
    search_fallback_root: Callable[[Mapping[str, Any]], Any] | None = None,
    plugin_modules: Iterable[ModuleType] = (),
) -> int:
    table = discover_verbs(plugins=plugin_modules, reserved=extra_verbs or ())
    for name, extra in (extra_verbs or {}).items():
        if name in table:
            raise RuntimeError(f"duplicate ledger verb: {name}")
        table[name] = extra
    parser = build_parser(table)
    args, extras = parser.parse_known_args(argv)
    args = _take_late_positionals(parser, args, extras)
    json_mode = bool(getattr(args, "json", False) or json_mode)
    db = args.db or _env("LEDGER_DB")
    if not db:
        raise UsageError("pass --db or set LEDGER_DB")
    verb: Verb = args._verb
    operator = _env("LEDGER_OPERATOR")
    agent = _env("LEDGER_AGENT")
    ctx = Context(
        db=db,
        json_mode=json_mode,
        actor=compose_actor(operator, agent),
        operator=operator,
        agent=agent,
        harness=_env("LEDGER_HARNESS"),
        model=_env("LEDGER_MODEL"),
        session=_env("LEDGER_SESSION"),
        roadmap_parser=roadmap_parser,
        search_fallback_root=search_fallback_root,
        program=_program_name(),
        db_explicit=_db_was_explicit(argv),
    )
    if verb.writes and not ctx.harness:
        raise CliError(
            "LEDGER_HARNESS is required for writes; set it, e.g. "
            "LEDGER_HARNESS=claude-code",
            code="missing_harness",
            exit_code=EXIT_REFUSED,
        )
    return _ok(json_mode, verb.run(args, ctx))


def main(
    argv: list[str] | None = None,
    *,
    roadmap_parser: Path | None = None,
    extra_verbs: Mapping[str, Verb] | None = None,
    search_fallback_root: Callable[[Mapping[str, Any]], Any] | None = None,
    plugin_modules: Iterable[ModuleType] = (),
) -> int:
    """Run the CLI. ``extra_verbs`` adds verbs a caller supplies to the
    discovered set; a name that is already discovered is refused.
    ``plugin_modules`` are verb modules the caller has imported; their verbs
    are appended after the leaf's, and a name already taken (by the leaf or an
    earlier plugin) is refused with exit 3, code ``verb_collision``.
    ``search_fallback_root`` gives the search index its markdown root when the
    ledger's config sets no ``search_roots``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    try:
        return _run(
            argv,
            json_mode,
            roadmap_parser,
            extra_verbs,
            search_fallback_root,
            plugin_modules,
        )
    except UsageError as exc:
        return _fail(json_mode, exc.code, str(exc), EXIT_USAGE)
    except CliError as exc:
        return _fail(json_mode, exc.code, str(exc), exc.exit_code)
    except UnsafeDatabaseFile as exc:
        return _fail(json_mode, exc.code, str(exc), EXIT_REFUSED)
    except SyncPathError as exc:
        return _fail(json_mode, "synced_path", str(exc), EXIT_REFUSED)
    except SharedDatabaseError as exc:
        return _fail(json_mode, "shared_database", str(exc), EXIT_REFUSED)
    except ForestRefusal as exc:
        return _fail(json_mode, exc.code, str(exc), EXIT_REFUSED)
    except ValidationError as exc:
        return _fail(json_mode, "validation", str(exc), EXIT_REFUSED)
    except IdempotencyConflict as exc:
        return _fail(json_mode, "conflict", str(exc), EXIT_REFUSED)
    except StaleRevision as exc:
        return _fail(json_mode, "stale_revision", str(exc), EXIT_STALE)
    except ChainBroken as exc:
        return _fail(json_mode, "integrity", str(exc), EXIT_INTEGRITY)
    except LedgerError as exc:
        message = str(exc)
        if "does not exist" in message:
            return _fail(json_mode, "missing_db", message, EXIT_NOT_FOUND)
        return _fail(json_mode, "error", message, EXIT_REFUSED)
    except sqlite3.Error as exc:
        return _fail(
            json_mode,
            "unusable",
            f"unusable database: {exc}",
            EXIT_REFUSED,
        )
    except BrokenPipeError:
        return EXIT_OK
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return EXIT_OK
        if type(code) is int:
            return code
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
