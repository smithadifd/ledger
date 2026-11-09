"""Pluggable ledger verbs.

Adding a verb is adding one module in this package and running
``python3 -m ledger.gen_verb_registry``, which regenerates ``_registry.py``.
The CLI loads exactly the listed modules; the list is static so that static
import analysis sees every verb. A module exports ``VERB`` and/or
``register(table)``. Names starting with ``_`` are skipped.

A caller can add verbs that live outside this package by passing plugin
modules (``discover_verbs(plugins=...)``, ``cli.main(plugin_modules=...)``).
The leaf never loads a module by name itself: the caller imports its plugins
and hands them over. A plugin only adds verbs; a name that is already taken is
refused.
"""
from __future__ import annotations

import re
import shlex
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping

from ledger.store import Event, Ledger, open_ledger

DEFAULT_LIMIT = 50

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_INTEGRITY = 4
EXIT_STALE = 5
_MAX_EVENT_ID = 2**63 - 1  # SQLite integer ceiling


class CliError(Exception):
    """A user-facing CLI failure with a stable error code and exit status."""

    def __init__(self, message: str, *, code: str, exit_code: int):
        self.code = code
        self.exit_code = exit_code
        super().__init__(message)


class UsageError(CliError):
    def __init__(self, message: str):
        super().__init__(message, code="usage", exit_code=EXIT_USAGE)


class PluginRefused(CliError):
    """A plugin verb would shadow a loaded verb, or is not a ``Verb``."""

    def __init__(self, message: str, *, code: str = "verb_collision"):
        super().__init__(message, code=code, exit_code=EXIT_REFUSED)


@dataclass(frozen=True)
class Verb:
    name: str
    help: str
    add_arguments: Callable[[Any], None]
    run: Callable[[Any, Context], Result]
    writes: bool = False
    # The name of a list-valued positional that may follow an option. Python
    # 3.11's argparse leaves such a positional over as an unrecognized
    # argument (3.12 fills it), so the CLI hands the leftovers to this one.
    late_positional: str | None = None


@dataclass
class Result:
    data: Any
    text: str = ""
    exit_code: int = EXIT_OK


@dataclass(frozen=True)
class Context:
    db: str
    json_mode: bool
    actor: str | None
    harness: str | None
    model: str | None
    session: str | None
    roadmap_parser: Path | None = None
    # The search index's fallback markdown root, supplied by the caller
    # (``ledger.search.FallbackRoot``); None means config search_roots only.
    search_fallback_root: Callable[[Mapping[str, Any]], Any] | None = None
    # The two parts ``actor`` is composed from (``compose_actor``): the
    # person (LEDGER_OPERATOR) and the seat (LEDGER_AGENT).
    operator: str | None = None
    agent: str | None = None
    # Command prefix shown in runnable output, and whether this invocation
    # supplied --db rather than LEDGER_DB.
    program: str = "ledger"
    db_explicit: bool = False


ACTOR_SEPARATOR = "/"


def invocation_prefix(program: str, db: str, db_explicit: bool) -> str:
    """Build the command prefix repeated in runnable CLI output."""
    if not db_explicit:
        return program
    return f"{program} --db {shlex.quote(db)}"


def compose_actor(operator: str | None, agent: str | None) -> str | None:
    """The stamped actor: ``<operator>/<agent>`` when both are set.

    DESIGN (SL38 D1): operator alone gives ``<operator>`` and agent alone
    gives ``<agent>`` (the single-operator form every older event has). A part
    containing the separator is refused, so the views' split (the text before
    the first ``/``) is never ambiguous.
    """
    for name, value in (("LEDGER_OPERATOR", operator), ("LEDGER_AGENT", agent)):
        if value and ACTOR_SEPARATOR in value:
            raise CliError(
                f"{name} must not contain {ACTOR_SEPARATOR!r} (got {value!r}): "
                "the actor is stamped as <operator>/<agent>",
                code="invalid_actor",
                exit_code=EXIT_REFUSED,
            )
    if operator and agent:
        return f"{operator}{ACTOR_SEPARATOR}{agent}"
    return operator or agent or None


def discover_verbs(
    modules: Iterable[ModuleType] | None = None,
    plugins: Iterable[ModuleType] = (),
    reserved: Iterable[str] = (),
) -> dict[str, Verb]:
    """Build the verb table from ``modules`` (default: the static registry),
    then append the verbs of each module in ``plugins``.

    A plugin module has a verb module's shape (``VERB`` and/or
    ``register(table)``). It can only add verbs: a name already in the table,
    from the leaf or from an earlier plugin, raises ``PluginRefused`` naming
    the verb and both owners. ``reserved`` names verbs the caller will add
    itself; a plugin naming one is refused the same way.
    """
    reserved = frozenset(reserved)
    if modules is None:
        # Imported here: every verb module imports this package.
        from ledger.verbs._registry import MODULES as modules
    table: dict[str, Verb] = {}
    for module in modules:
        short = module.__name__.rsplit(".", 1)[-1]
        if short.startswith("_"):
            continue
        register = getattr(module, "register", None)
        if callable(register):
            register(table)
            continue
        verb = getattr(module, "VERB", None)
        if not isinstance(verb, Verb):
            continue
        if verb.name in table:
            raise RuntimeError(f"duplicate ledger verb: {verb.name}")
        table[verb.name] = verb
    owners: dict[str, str] = {}
    for plugin in plugins:
        for name, verb in _plugin_verbs(plugin).items():
            if name in table or name in reserved:
                owner = owners.get(name)
                if owner:
                    taken = f"plugin {owner}"
                elif name in table:
                    taken = "the ledger"
                else:
                    taken = "the caller"
                raise PluginRefused(
                    f"ledger plugin {plugin.__name__} cannot add verb {name!r}: "
                    f"{taken} already provides it; a plugin can only add verbs"
                )
            table[name] = verb
            owners[name] = plugin.__name__
    return table


def _plugin_verbs(plugin: ModuleType) -> dict[str, Verb]:
    """The verbs one plugin module adds, staged apart from the loaded table so
    that its ``register`` hook can never overwrite a loaded verb."""
    staged: dict[str, Verb] = {}
    short = plugin.__name__.rsplit(".", 1)[-1]
    if short.startswith("_"):
        return staged
    register = getattr(plugin, "register", None)
    if callable(register):
        register(staged)
    else:
        if hasattr(plugin, "VERB"):
            verb = plugin.VERB
            if not isinstance(verb, Verb):
                raise PluginRefused(
                    f"ledger plugin {plugin.__name__} exports VERB as "
                    f"{type(verb).__name__}, not a ledger Verb",
                    code="invalid_plugin",
                )
            staged[verb.name] = verb
    for name, verb in staged.items():
        if not isinstance(verb, Verb):
            raise PluginRefused(
                f"ledger plugin {plugin.__name__} registered {name!r} as "
                f"{type(verb).__name__}, not a ledger Verb",
                code="invalid_plugin",
            )
    return staged


def event_dict(event: Event) -> dict[str, Any]:
    return asdict(event)


def item_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": row["item_id"],
        "type": row["type"],
        "title": row["title"],
        "status": row["status"],
        "owner": row["owner"],
        "claim": row["claim"],
        "created_event_id": row["created_event_id"],
        "state_event_id": row["state_event_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def format_items(items: list[Mapping[str, Any]]) -> str:
    return "\n".join(
        f"{it['item_id']}\t{it['type']}\t{it['status']}\t{it['title']}" for it in items
    )


def add_expect_revision_argument(parser) -> None:
    parser.add_argument(
        "--expect-revision",
        metavar="EVENT_ID|none",
        help=(
            "append only if the item's newest event id equals this (or 'none' "
            "for an item with no events); refuses with exit 5 otherwise"
        ),
    )


def expect_revision_value(args) -> int | str | None:
    """Parse ``--expect-revision``: a positive event id, ``none``, or unset."""
    raw = getattr(args, "expect_revision", None)
    if raw is None:
        return None
    if raw == "none":
        return "none"
    if re.fullmatch(r"[1-9][0-9]*", raw) is None:
        raise UsageError(
            f"--expect-revision must be an event id or 'none', got {raw!r}"
        )
    try:
        value = int(raw)
    except ValueError:  # digits beyond Python's int-conversion limit
        value = None
    if value is None or value > _MAX_EVENT_ID:
        raise UsageError("--expect-revision is too large to be an event id")
    return value


def new_key() -> str:
    return uuid.uuid4().hex


def actor_kwargs(ctx: Context) -> dict[str, str | None]:
    return {
        "actor": ctx.actor,
        "harness": ctx.harness,
        "model": ctx.model,
        "session": ctx.session,
    }


def open_db(ctx: Context, *, write: bool) -> Ledger:
    return open_ledger(ctx.db, create=write)


def resolve_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if type(value) is not int or value < 1:
        raise UsageError("--limit must be an integer >= 1")
    return value


def require_item(lg: Ledger, item_id: str) -> dict[str, Any]:
    for row in lg.items():
        if row["item_id"] == item_id:
            return item_dict(row)
    raise CliError(f"item not found: {item_id}", code="not_found", exit_code=EXIT_NOT_FOUND)
