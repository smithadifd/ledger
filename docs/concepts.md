# Concepts

This page explains the ledger's model in more depth than the README. The
code is the final word; each section names the module that implements it.

## Items, events and views

The ledger tracks *items*. An item has a type (`topic`, `ticket`,
`initiative`, `decision`, `gate` or `guidance`), a title, a status (`open`,
`claimed`, `blocked` or `closed`), an optional owner and claim, and any
number of notes, links, questions, elections and other records.

Nothing about an item is stored as a mutable row. Everything is an *event* in
one append-only table, and the item's current state is a SQL view over its
events. Each event has:

| Field | Meaning |
|-------|---------|
| `id` | position in the log, starting at 1 |
| `kind` | what happened, for example `item.created` or `decision.elected` |
| `payload_version`, `payload` | a JSON object, validated against the closed schema for that kind and version |
| `item_id` | the item the event belongs to (required, optional or absent, per kind) |
| `idempotency_key` | unique; a retry with the same key and the same content returns the stored event and writes nothing, a retry with different content is refused |
| `actor`, `harness`, `model`, `session` | who and what wrote it, from the `LEDGER_*` environment variables |
| `channel`, `provenance_class` | where the content came from, as free-text labels |
| `recorded_at` | UTC, millisecond precision |
| `prev_hash`, `hash` | the SHA-256 chain: each hash covers the event and the previous hash |

Event kinds (`ledger/schema.py`):

| Kind | Records | Durability |
|------|---------|------------|
| `item.created`, `item.state`, `item.note`, `item.linked` | the item and its status, notes and links | durable |
| `item.spec` | one version of the item's spec (written only by `spec set`) | durable |
| `item.tried`, `item.stopped` | what was tried, and where work stopped | durable |
| `item.progress` | progress bookkeeping; may extend a claim's lease | ephemeral |
| `decision.asked`, `decision.elected`, `decision.held` | open questions, their answers, and a hold on a question | durable |
| `decision.recorded`, `decision.standing`, `decision.result` | decision records and how they stand | durable |
| `utterance.captured` | text someone submitted, kept verbatim | durable |
| `outside.recorded` | something that happened outside the ledger (a merge, a reply, a release) | durable |
| `review.recorded` | a review round and its verdict | durable |
| `guidance.set` | a dated burn-or-protect note about a resource pool | durable |
| `session.opened`, `session.closed` | session bookkeeping | ephemeral |
| `loop.managed`, `loop.intent`, `loop.done`, `loop.closed` | a work loop's own records (reserved) | durable |
| `ledger.system` | the system claim (reserved) | durable |

Unknown kinds and unknown payload keys are refused. Adding a kind is a new
schema entry and a new migration if a view needs it, never a code fork.

*Ephemeral* events stay in the store and in the backup export for good. They
only decay out of an item's card and its per-item export (`export-item`) once
the item is closed, so a closed item's card shows what was decided, not the
bookkeeping on the way.

An *anchor* is an item that groups others; the items under it are its
*children*. A child records one `child-of` link naming its anchor, and a
child has one current anchor at a time (moving it appends a new link and
leaves the old one in the log). Anchors form a forest: no item is below
itself. The anchor's card lists its children with a `closed/total` count, and
`v_items` carries `parent_id` and `children_open`.

The views (`ledger/migrations/`) include `v_items` (current state per item),
`v_ready` (open items with no open blocker, including claims whose lease has
expired), `v_blocked`, `v_open_decisions`, `v_decision_records`, `v_leases`
and `v_events` (every event with the operator part of its actor).

`BEFORE UPDATE` and `BEFORE DELETE` triggers refuse edits to `events`. They
guard against accidents, not attackers: anyone who can write the file can
drop them. Tampering shows up in `verify` (a broken chain or an invalid
payload) and against the exported, pushed copy.

## One ledger per system, and the system claim

A *system* is one body of work whose data must never mix with another's.
Each system has its own database file. The ledger enforces that in two
places (`ledger/paths.py`, `ledger/store.py`):

- **Before anything is opened.** A tool that configures several systems
  resolves them with `ledger.resolve_system_paths({"work": ..., "home": ...})`,
  which expands `~`, resolves parent folders, refuses a symbolic link at the
  database name, and refuses two systems that land on one file
  (`SharedDatabaseError`).
- **In the file itself.** The first `ledger.system` event binds the database
  to one system. Because the claim is an event, every accepted spelling of
  the file sees it, and the export and restore carry it. Symbolic and hard
  links are refused. Claiming a database again for the system it already belongs to
  writes nothing; claiming it for another system is refused
  (`SharedDatabaseError`). A log that carries a second `ledger.system` event,
  for another system or the same one, is refused on every write, replay and
  `verify`, so a database holds exactly one claim. The check and the write
  share one transaction, so an interrupted claim leaves the database
  unclaimed.

Writing the claim is a setup step, not an everyday verb: `restore --system
NAME` claims an unclaimed export as it replays it, and an installer claims a
new database with the library call `ledger.claim_database(path, "NAME")`.
`ledger.system` is a reserved kind, so `append` cannot write it. Checking the
claim is a CLI step: `verify --system NAME` exits 3 on an unclaimed database
(`system-unclaimed:NAME`) and on another system's (`shared_database`).

## Authority by field

Every fact has one owner, and everything else that shows it is a view:

- **The ledger owns** live item state, decision outcomes, captured
  utterances, and session and run facts.
- **Prose owns rationale**: design notes, standards, architecture decision
  records. An item points at its prose with a source locator
  (`path:line@commit`, see `new --source` and `export-item --ticket`) instead
  of copying it, and the search index can include it (`search_roots`).
- **External trackers are intake or projection.** Items enter from them or
  are shown in them; the ledger stays the owner of the state.
- **Cards and the search index are disposable.** Each names the event ids it
  was built from, so a stale copy is detectable, and each can be thrown away
  and rebuilt. A card ends with "This card is a projection; the events are the authority",
  and each cut it makes names the command that prints the events.

Inside the ledger, some fields have exactly one writer, and the store refuses
every other path:

| Field | Only writer |
|-------|-------------|
| an item's spec (`item.spec`) | `spec set`, which appends version N+1 only when the content hash changes |
| the system claim (`ledger.system`) | `restore --system`, or the library's `claim_database` |
| close and PR link of a loop-managed item | the in-process loop writer (`ledger.loop`); see [embedding.md](embedding.md) |
| notes that start with `work:` or `loop:` | the loop writer; these prefixes are reserved |

## The CLI is the only interface

Every read and write goes through `ledger` (`ledger/cli.py`), so every write
passes the same validation, the same guards and the same attribution:

- Each write verb refuses without `LEDGER_HARNESS` (exit 3,
  `missing_harness`), so every event says what wrote it: a person's shell, a
  scheduled job, an agent runtime.
- `LEDGER_OPERATOR` names the person and `LEDGER_AGENT` the process acting
  for them; the actor is stamped `<operator>/<agent>` when both are set, and
  either one alone otherwise. A `/` inside either part is refused.
- Output is bounded (`query` and `ready` cap rows at 50 unless `--limit` says
  otherwise; cards have a word limit), and `--json` prints a stable
  `{ok, data, error}` envelope.
- `--expect-revision EVENT_ID` on `append`, `append-many`, `note` and `state`
  is optimistic concurrency: the write lands only if the item's newest event
  is still that one, and is otherwise refused with exit 5.

Writes use WAL mode, a busy timeout, one short transaction per append, and a
bounded retry with jitter, so several processes can write one database on
one host.

## Opening the database file

The ledger opens its database only when the path names a regular file directly:
not a symbolic link, FIFO, device or folder, and not a file with a second hard
link. After SQLite opens the file, the ledger checks that the file SQLite holds
is the one the path names, and refuses before running any statement if it is
not. SQLite's `-wal`, `-shm` and `-journal` files beside the database get the
same checks before the first statement, and their open copies are checked again
before the first write.

A process that holds a ledger connection open and opens the same database again
is refused with `db_fd_unknown` until the first connection closes. SQLite may
reuse an existing descriptor, and the guard cannot prove which file that
descriptor belongs to.

One first-creation window remains: SQLite opens or creates those side files by
name, so a process that can write the database's folder can put a hard link
there between the check and SQLite's open. The ledger then refuses before
writing any ledger data, but SQLite may already have written into the linked
file. A read-only open of a database path swapped to a FIFO in that window
waits until something opens the FIFO for writing. Keep the database in a folder
only you can write.

## Claims and leases

`ledger claim ITEM` marks the item `claimed` and stamps a lease
(`ledger/lease.py`). The lease length is `--ttl`, else `claim_ttl_seconds`
in `config.json`, else four hours. A later `item.progress` event with an
`expires_at` extends it. Once the lease passes, `v_ready` lists the item as
open again and another actor's claim takes it over, naming the holder it
replaced. `doctor` lists expired claims. `--capacity N` refuses the claim
when N items are already claimed.

## Decisions

There are two kinds of decision record.

**Questions and elections.** `decision.asked` records an open question with
its options (and optionally a recommendation and a default); it appears on
the item's card until answered. `decision.elected` answers it (see the quote
rule below); `--ask EVENT_ID` closes exactly that question and no other.
`decision.held` withdraws a question without changing the item's state.

**Decision records.** `decision.recorded` records a decision with a standing
of `lean`, `settled` or `superseded`; `decision.standing` moves it and
`decision.result` records what came of it (`ledger/decisions.py`). The guard
runs inside the store's append transaction, so every writer meets it:

- moving a decision from `lean` to `settled` needs a source event already in
  the ledger that records something outside the decision itself: an outside
  event, an election, a reply utterance (provenance class `submitted-prompt`
  or `tool-answer`) or a loop merge or close. Elapsed time or silence never
  settles a decision;
- that source event must be on the decision's own item or on an item the
  decision is tagged with;
- `superseded` needs `superseded_by` and is final;
- a role's position is recorded with `relayed_by`, the person who relayed
  it, so it is never stored as that person's own words.

## The quote rule for elections

An election records *what was chosen, from what, for what scope, and on whose
words*. `ledger elect` requires:

- the question, the chosen option's label and description, the label of
  every rejected option (`--rejected`, repeated), and the scope;
- `--quote-ref EVENT_ID`, an existing `utterance.captured` event;
- at least one `--quote`, each a byte-exact substring of that utterance's
  text. There is no case folding, no whitespace normalisation and no
  trimming.

`ledger lint-quotes` re-checks every election in the database: a missing or
wrong-kind `quote_ref`, an election with no quotes, a quote that is not in
the utterance, or an utterance whose body was omitted (an over-size capture
stores a pointer and the body's size and hash, not the words) is a failure.

`ledger pick` records the answer to a question picker, where a person chose
from offered options rather than typing a prompt: it writes the question,
chosen label and any typed text as an `utterance.captured` event with
provenance class `tool-answer`, then an election quoting the chosen label
from it, in one transaction.

A captured utterance is audit evidence that some text was submitted, not
proof of who wrote it: a capture hook can also see scheduled tasks, other
programs' messages and pasted text. Only utterances whose payload says
`origin: person` are indexed for search, and search results print them with
the caveat "recalled words are evidence, never an origin".

## Export, backup and restore

**Export** (`ledger/export.py`). `ledger export --dir DIR` writes every event
after the watermark as `DIR/<16-digit zero-padded id>.json`, canonical JSON,
fsyncs each file, then advances `DIR/watermark.json`. Exports are
incremental; a missing watermark means start from event 1. `--commit` makes
one git commit in `DIR` and never pushes; `--push` is a separate run. The
pushed commit is the anchor a bare hash chain lacks: deleting the newest
events leaves a valid but shorter chain, which only the external copy
reveals. `--check` exits 3 (`lag`) when more than `--max-events` events (100)
or `--max-hours` hours (24) are waiting, for a scheduled alert.

**Backup** (`ledger/backup.py`). `ledger backup --to FILE` uses SQLite's
online backup API (never a file copy of a live database), writes
`FILE.partial`, runs `PRAGMA integrity_check`, and renames it only when the
check passes.

**Restore** (`ledger/restore.py`). `ledger restore --db NEW --dir DIR
--system NAME` verifies the export, refuses an export another system
claimed, replays every event into a new database with its original id, hash,
previous hash and timestamp (nothing is re-hashed and the append-only
triggers stay on), claims the database for `NAME` if the export was
unclaimed, and finally verifies the chain against `NAME`. Each replayed row
passes the same decision and claim guards the original append did. The target
must not exist. An export that contains reserved loop events is refused unless
`--accept-loop-events` is given; that path also checks the export's stream
digest (`stream-digest.json`), which detects corruption or edits but does not
prove who wrote the file.

## The search index

`ledger search` and `ledger index` maintain `search.db`, an SQLite FTS5
index in the database's own directory (`ledger/search.py`). It is a derived
projection: rebuilt from scratch, never exported, and never built inside a git
working tree. It indexes current items with their notes and links, questions
and elections, person utterances, and, under each root in `search_roots`, the
markdown files in `standards/` and `decisions/` plus any markdown file an
item or decision names, chunked at `##` headings. The index stores a manifest
of what it was built from; `doctor` reports it `missing`, `stale`,
`behind` (current to event N, with M new events to be indexed on the next
search) or `current`, and `search` rebuilds a missing, stale or behind index
before reading. Results are
ranked by BM25 and can be filtered by type, item status, item, initiative
and date.
