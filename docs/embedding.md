# Embedding the ledger

The ledger is meant to be driven through its CLI. A larger tool that wants
its own command name, extra verbs, or an automated work loop can embed it in
the ways below. None of them is a second way to write events: every path
still goes through the store's validation and guards.

## Your own entry point

`ledger.cli.main` is the whole CLI. A wrapper script can call it with extra
arguments:

```python
#!/usr/bin/env python3
import sys

from ledger import cli
import my_verbs  # a module shaped like a verb module (see below)

if __name__ == "__main__":
    sys.exit(cli.main(plugin_modules=[my_verbs]))
```

`cli.main(argv=None, *, plugin_modules=(), extra_verbs=None,
search_fallback_root=None, roadmap_parser=None)`:

- `plugin_modules`: modules the caller has already imported, each adding
  verbs (below).
- `extra_verbs`: a mapping of name to `ledger.verbs.Verb` for verbs built in
  code. A name the ledger already has raises an error, and a plugin that
  names one of these is refused.
- `search_fallback_root`: a callable given the database's `config.json`
  contents that returns a `ledger.search.Root` (a markdown root with its
  `standards/` and `decisions/` folders) when the config sets no
  `search_roots`, or raises `LookupError` saying how to set one. Without it,
  a config with no `search_roots` indexes only the ledger.
- `roadmap_parser`: a path handed to verbs through `Context.roadmap_parser`
  for plugin verbs that parse a project's own planning files. The ledger's
  own verbs do not use it.

## Verb plugins

A verb module exports `VERB`, a `ledger.verbs.Verb`:

```python
from ledger.verbs import Context, Result, Verb


def add_arguments(parser) -> None:
    parser.add_argument("item")


def run(args, ctx: Context) -> Result:
    return Result(data={"item": args.item}, text=f"hello {args.item}")


VERB = Verb(name="hello", help="say hello", add_arguments=add_arguments, run=run)
```

or a `register(table)` function that adds several. Set `writes=True` on a
verb that writes, so the CLI requires `LEDGER_HARNESS` before running it.
Rules:

- The ledger never imports a plugin by name: the caller imports it and
  passes the module.
- A plugin only adds verbs. A name that the ledger, the caller or an earlier
  plugin already provides is refused with exit 3, code `verb_collision`; an
  export that is not a `Verb` is refused with `invalid_plugin`.
- Modules whose name starts with `_` are skipped.

The ledger's own verbs live in `ledger/verbs/` and are listed statically in
`ledger/verbs/_registry.py`, so that tools which follow static imports see
every verb the CLI can load. After adding or removing a module there, run
`python3 -m ledger.gen_verb_registry` (from the folder that holds `ledger/`)
to regenerate the list; `--check` compares without writing and exits 1 when
the list is out of date.

## Attribution: `LEDGER_HARNESS` and friends

Every write verb refuses to run without `LEDGER_HARNESS`. Set it to whatever
names the writing tool (`cli` for a person's shell, the name of a scheduled
job, an agent runtime). Together with `LEDGER_OPERATOR`, `LEDGER_AGENT`,
`LEDGER_MODEL` and `LEDGER_SESSION` it is stamped on every event, so the log
always says who and what wrote each line. These are attribution, not
authentication: anything that can run the CLI can set them.

## Work loops and the reserved kinds

An automated *work loop* (a process that picks an item, works it, has it
reviewed and closes it) needs a few writes no person or script should make
by hand. They are reserved event kinds, written only through the in-process
writer in `ledger/loop.py`:

| Kind | Meaning |
|------|---------|
| `loop.managed` | the item is handed to the loop |
| `loop.intent` | the loop is about to perform an external side effect, one of `build`, `decision`, `review`, `merge` or `ping` |
| `loop.done` | that side effect finished, with its result |
| `loop.closed` | the loop closed the item |

Each side effect is an intent before it runs and a done event after it
finishes, so a resumed loop reads `operation_state` and reconciles a started
operation instead of repeating it. Once an item is loop-managed:

- only `loop.closed` closes it: `ledger state --status closed` is refused;
- only the loop links its PR: `ledger link --rel pr` is refused;
- notes starting with `work:` or `loop:` are reserved for it.

The guarantee is about the process boundary: no sequence of CLI write verbs
can write a reserved kind, close a loop-managed item or link its PR.
In-process callers that import `ledger.loop` are trusted. `restore` refuses
an export that contains loop events unless `--accept-loop-events` is passed.

## Routing keys on items

`item.created` takes two optional keys for a scheduler that runs work
unattended: `queue`, a letters-only name of the queue the item belongs to,
and `target_repo`, the name of the repository the work happens in. The ledger
stores and validates them; it does not act on them. `ledger new` does not set
them; write the creation event with `ledger append --kind item.created` when
you need them.

## Held questions

An integration that mirrors open questions to a person (a notification
queue, a digest) may need to park a question without answering it. Two ways
exist:

- a `decision.held` event with `status: held` and the question's
  `asked_event_id` withdraws that one question from `v_open_decisions` until a
  later `released` marker;
- an `item.state` of `blocked` whose idempotency key starts with
  `needs-you:<item>:item.state:blocked:` is that integration's own block: it
  withdraws the questions recorded before it, and a work loop will not take
  the item while it stands. A block a person sets with `ledger state` withdraws
  nothing.

## Conversation pointers on cards

Set `LEDGER_POINTER_SIDECAR` to an executable that searches past
conversations. The card runs it with the item id as its only argument (no
shell) and expects a JSON array of `{"ref": ..., "note": ...}` objects on
stdout; they are shown under `## Conversation pointers`. A sidecar that fails
or prints anything else yields one pointer saying so, never a failed card.

## Library calls

The package is importable (`import ledger`) for setup steps and for embedding;
day-to-day reads and writes should go through the CLI. The setup calls are:

- `ledger.resolve_system_paths({name: path, ...})`: resolve every system's
  database path and refuse two that share a file;
- `ledger.claim_database(path, system, *, actor=None, harness=None, model=None, session=None)`: bind a database to its system, stamping the claim with the caller's attribution when given;
- `ledger.resolve_db_path(path)`: expand and resolve a path, refusing a
  synced folder.
