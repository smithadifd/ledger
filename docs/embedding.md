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
| `loop.intent` | the loop is about to perform an external side effect, one of `build`, `decision`, `review`, `merge` or `ping`; payload version 2 is a hand-off (below) |
| `loop.done` | that side effect finished, with its result |
| `loop.closed` | the loop closed the item |

Each side effect is an intent before it runs and a done event after it
finishes, so a resumed loop reads `operation_state` and reconciles a started
operation instead of repeating it. Once an item is loop-managed:

- only `loop.closed` closes it: `ledger state --status closed` is refused;
- only the loop links its PR (`link_pull_request`, below): `ledger link --rel pr`
  and every other generic writer are refused `reserved-transition:loop-pr`;
- notes starting with `work:` or `loop:` are reserved for it.

The guarantee is about the process boundary: no sequence of CLI write verbs
can write a reserved kind, close a loop-managed item or link its PR.
In-process callers that import `ledger.loop` are trusted. `restore` refuses
an export that contains loop events unless `--accept-loop-events` is passed.

The review kinds `review.receipt`, `review.grant-cite`, `review.merge-intent`
and `review.merge-done` are reserved the same way, by exact name
(`review.recorded` stays an ordinary kind), and
written only through the in-process receipt writer in `ledger/receipts.py`:
`write_receipt` records one review round of one pull request on the one item
whose `pr` link names it (`pr_item`), with its `review-receipt` document
attached inline in the same transaction (an identical document already on the
item stored by path is refused `doc_not_inline`) and the document's
ledger-computed sha256 as `doc_sha256`; `write_grant_cite` records that `grant:review-round-past-cap`
was standing (`ledger.verbs.grants.standing`) when it wrote. A
`review-receipt` document added with `doc add` is not a receipt. `restore`
refuses an export holding any of these kinds unless the same `--accept-loop-events`
is passed, which also checks the export's stream digest.

### Hand-offs

A `loop.intent` of payload version 2 is a *hand-off*: it records who
controls a build and which implementation runs each of the loop's seams for
as long as the build is open. Only `claim_operation` with `hand_off=True`,
`controller` and `seams` writes one, on a `build` operation, as the holder
of the item's live claim, in the same transaction that leaves that claim
without an expiry:

- `controller` is one token: ASCII letters, digits, `.`, `_` and `-`,
  starting with a letter or digit, 1 to 64 characters. It names a role, and
  a reader compares it by exact equality.
- `seams` is an object with exactly the keys `build`, `review` and
  `outward`, each one token of the same shape.
- Both are required, and nothing else is added to version 1's keys. There is
  no `parent` key: the item's `child-of` link gives the parent. Version 1 is
  unchanged and takes neither key; `claim_operation` without the fields
  writes it exactly as before.

Each refusal writes nothing, neither the intent nor the claim:
`missing-key:controller` or `missing-key:seams` (one without the other),
`hand-off-fields-need-hand-off` (either without `hand_off=True`),
`bad-type:operation` (an operation other than `build`), `bad-type:controller`,
`too-long:controller`, `bad-type:seams`, `missing-key:seams.<seam>`,
`unknown-key:seams.<name>` and `bad-type:seams.<seam>` (a field off its
grammar), `unknown-key:<key>` (any other key), `loop-item-not-managed:<item>`,
`claim_lost` (the caller does not hold the item's live claim, or the item is
closed) and `loop-hand-off-open:<operation id>` (another hand-off on the item
is open). A retry under the same operation id with another controller, other
seams or the other version is an `IdempotencyConflict`; an exact retry
returns the stored event. The generic writers refuse the kind
(`reserved-kind:loop.intent`) whatever its version.

**Reader rule.** An item's hand-off is its one version 2 `loop.intent` with
no `loop.done` naming the same operation and operation id. With none, the
item is not handed off: the person controls it and the seams are the
defaults. A version 1 intent is never a hand-off. In process,
`ledger.loop.open_hand_off(ledger, item_id)` applies the rule: it returns
the event or `None`, and refuses `loop-hand-off-ambiguous:<item>` when two
are open (a restored chain can hold two; the writer cannot). The writer's
refusal of a second hand-off reads through the same code. From the CLI, read
the item with `export-item --json`, which prints each event's
`payload_version`; `events --json` does not print it.

A ledger from before hand-offs (0.6.3 and earlier) refuses a database or
export holding one: `verify`, `verify_export` and
`restore --accept-loop-events` stop with `unknown-version:loop.intent:2`, and
`export --commit` refuses the record. Its `export-item` prints the event
without checking it. Upgrade every reader before anything writes a hand-off.

### The pull request link

`ledger.loop.link_pull_request(ledger, item_id, holder, repo, pr)` records
the pull request of a loop-managed item. It is the only writer of an
`item.linked` with rel `pr` on such an item, and it writes an ordinary one:
payload version 1, exactly `{"rel": "pr", "target": <url>}`, where the URL is
`ledger.receipts.pr_url(repo, pr)`, the lowercase
`https://github.com/<owner>/<repo>/pull/<N>` that `pr_item`, the receipt
writer and the merge records look up. The key is
`loop:<item>:pr-link:<url>`. It returns the event and whether this call
wrote it (`created`). No CLI verb reaches it, so the guarantee above stands.

- `holder` is required, a non-empty string (`loop-pr-link-needs-holder`).
- `repo` is a lowercase `owner/repo` under the receipt's rule, at most 200
  characters; an uppercase letter is refused, never folded (`bad-type:repo`,
  `too-long:repo`).
- `pr` is an integer from 1 to 2^63-1 (`bad-type:pr`, `too-large:pr`).

These are checked before the write. Then, in the write's transaction: the
item must be loop-managed (`loop-item-not-managed:<item>`); `holder` must
hold the item's live claim (`claim_lost`, also for a closed item or a lease
that ran out); the item must have no `pr` link at all, whatever its target,
form or key (`loop-pr-link-already-exists:<item>`); and no other item may
link the same pull request, compared as `pr_item` compares (the URL or the
`owner/repo#N` form, owner and repo in any case;
`loop-pr-linked-elsewhere:<that item>`). Every refusal writes nothing. The
same call again returns the stored event with `created` false and writes
nothing, whatever the actor, session or holder of the retry, so `created`
false is not proof of holding the claim; another event already under the key
is an `IdempotencyConflict`. The hand-off is not read: an item linked after
its hand-off finished, or under a new holder's claim, links the same way.
`manage_item` still refuses an item that already has a `pr` link, so a link a
generic writer made before the loop took the item is never adopted.

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
