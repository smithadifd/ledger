# ledger

An append-only, hash-chained event log in one SQLite file, for tracking work
items, the decisions made about them, and the words those decisions rest on.
Every change is a new event; current state is read from SQL views over the
events. One command, `ledger`, is the interface for people, scripts and
automated agents alike.

- **Standard library only.** Python 3.11 or newer and its `sqlite3` module.
  The views use SQLite's JSON functions and the search index uses FTS5, both
  built into most Python distributions. No runtime dependencies.
- **Local.** One database file per system, on local disk. The ledger refuses
  to open a database inside a synced folder (iCloud, Dropbox, OneDrive,
  Syncthing), because sync tools corrupt live SQLite files.
- **Durable.** An incremental export writes one canonical JSON file per
  event, ready to commit to a private git repository; restore replays that
  export into a fresh database and checks the hash chain.

## Quick start

Run from a clone of this repository (no install step needed):

```sh
mkdir -p /tmp/ledger-demo
export LEDGER_DB=/tmp/ledger-demo/ledger.db   # or pass --db on every call
export LEDGER_HARNESS=cli                     # required for writes: what is writing
export LEDGER_AGENT=me                        # optional: who is writing

python3 -m ledger new --type ticket --title "Write the quick start" --id demo-1
python3 -m ledger doctor
python3 -m ledger note demo-1 --text "First draft of the quick start is done"
python3 -m ledger append --item demo-1 --kind item.progress \
  --payload '{"text": "examples checked against a fresh database"}'
python3 -m ledger card demo-1
python3 -m ledger search draft
python3 -m ledger verify
```

The first write creates the database and applies every migration. `doctor`
on a path that does not exist yet exits 1 (`database does not exist`); after
the first write it prints:

```text
path: /tmp/ledger-demo/ledger.db
schema_version: 8
search: missing (ledger index rebuilds it)
```

`card` prints a bounded resume card for the item:

```text
# demo-1
revision: 3 <hash of event 3>
source_event_ids: 1, 2, 3
type: ticket
title: Write the quick start
status: open
...
## Notes
- event=2: First draft of the quick start is done

## Progress
- event=3: examples checked against a fresh database

This card is a projection, not authority
```

`search` builds a derived full-text index (`search.db`, beside the database)
on first use and ranks matches with BM25. `verify` walks the hash chain and
re-validates every payload (`ok events=3`).

Installing with `pip install .` from a clone provides the same command as
`ledger` on your `PATH`; `ledger <verb>` and `python3 -m ledger <verb>` are
equivalent.

## What the ledger is

The ledger keeps a small number of promises. Each has a longer explanation in
[docs/concepts.md](docs/concepts.md).

**One ledger per system, and the system claim.** A *system* is one body of
work whose data must never mix with another's: say, a personal setup and an
employer's. Each system has its own database file, and the file itself
records which system it belongs to, as a `ledger.system` claim event. A
database carries exactly one claim, and a claim for another system is
refused. Because the claim lives inside the file, a symlink or a second
spelling of the path still finds it. `verify --system NAME` fails unless the
database is claimed for `NAME`, and `restore --system NAME` refuses an export
that another system claimed.

**Events only.** The `events` table is append-only: SQLite triggers refuse
`UPDATE` and `DELETE`. Every event names its kind, carries a payload checked
against a closed schema for that kind, and is chained to the previous event
by SHA-256. A correction is a new event, never an edit. Items, open
questions, leases and decision records are SQL views over the events. The
triggers are an accident guard, not a security boundary: anyone who can
write the file can replace it. `verify`, the export and its git history are
what make tampering visible.

**Authority by field.** Every fact has one owner. The ledger owns live item
state, decision outcomes, captured utterances and session facts. Prose
(design notes, standards, architecture decision records) owns rationale; an
item points at it with a source locator rather than copying it. External
trackers are intake surfaces or read-only projections. Resume cards and the
search index are disposable views that name the events they were built from;
they are never authority. Inside the ledger, some fields have exactly one
writer: an item's spec is written only by `spec set`, and an item handed to a
work loop is closed only by that loop.

**The CLI is the only interface.** People, scripts and agents read and write
through `ledger`; nothing else opens the database. Output is bounded, and
`--json` gives a stable `{ok, data, error}` envelope for machines. Every
write needs `LEDGER_HARNESS`, so every event says what wrote it. The Python
package is importable for embedding the CLI in a larger tool (see
[docs/embedding.md](docs/embedding.md)), but not as a second way to write.

**Export and restore.** `ledger export --dir DIR` writes every event after a
watermark as `DIR/<16-digit id>.json` plus `watermark.json`; `--commit` makes
one git commit there and never pushes, and `--push` pushes as a separate
step. `--check` exits non-zero when the export lags too far behind. A bare
hash chain cannot show that its newest events were cut off; the pushed export
commit is that external anchor. `ledger restore --dir DIR --system NAME`
replays an export into a new database with the original ids and hashes, then
verifies the chain. `ledger backup --to FILE` takes an online SQLite backup
and checks its integrity before keeping it.

**The quote rule for elections.** An *election* (`decision.elected`) records
a choice with five fields of provenance (the question, the chosen option's
label and description, every rejected option, and the scope) plus a
`quote_ref` to a captured utterance and at least one `--quote`. Each quote
must be a byte-exact substring of that utterance's text: no case folding and
no whitespace normalisation. `ledger lint-quotes` re-checks every election.
A captured utterance is evidence of what text was submitted, not proof of who
wrote it.

```sh
python3 -m ledger append --item demo-1 --kind utterance.captured \
  --channel terminal --provenance-class submitted-prompt \
  --payload '{"text": "Ship the short version first.", "origin": "person"}'
# appended  utterance.captured  event=4  item=demo-1
python3 -m ledger elect demo-1 --question "Short or long quick start?" \
  --chosen short --chosen-description "five commands" --rejected long \
  --scope "the README" --quote-ref 4 --quote "short version"
```

A quote that is not in the utterance (`--quote "shorter version"`) is refused
with exit 3: `quote is not a verbatim substring of utterance 4`.

## Verbs

| Verb | What it does | Writes |
|------|--------------|:------:|
| `new` | create an item (`topic`, `ticket`, `initiative`, `decision`, `gate`, `guidance`) | yes |
| `note` | append a note to an item | yes |
| `state` | set an item's status (`open`, `claimed`, `blocked`, `closed`) | yes |
| `claim` | claim an item with a lease, optionally capped by `--capacity` | yes |
| `link` | link an item to a PR, branch, session, URL or another item; `duplicate-of` also closes it | yes |
| `append` | append a validated event of any non-reserved kind | yes |
| `append-many` | append several events for one item in one transaction (JSON array on stdin) | yes |
| `spec` | `set`, `show` or `diff` an item's versioned spec | `set` |
| `elect` | record an election with quote provenance | yes |
| `pick` | record a question-picker answer as an utterance plus an election | yes |
| `guidance` | `set`, `list` or `withdraw` a dated burn-or-protect note for a resource pool | `set`, `withdraw` |
| `query` | list current items, with filters and a row cap | no |
| `ready` | list open items with no open blocker | no |
| `card` | render a bounded resume card for an item | no |
| `decisions` | list decision records with their current standing | no |
| `export-item` | print one item's events (ephemeral ones decay after close) | no |
| `search` | full-text search over items, decisions, utterances and markdown roots | no |
| `index` | build or rebuild the derived search index | no |
| `lint-quotes` | check every election's quotes against its utterance | no |
| `verify` | verify the hash chain and every payload; `--system` checks the claim | no |
| `doctor` | print the database path, schema version, search-index state and expired claims | no |
| `export` | write events after the watermark; `--commit`, `--push`, `--check` | no |
| `backup` | online backup with an integrity check | no |
| `restore` | replay an export into a fresh database | new file |

`ledger --help` and `ledger <verb> --help` list every flag. The global flags
`--db` and `--json` may come before or after the verb. Full reference:
[docs/cli.md](docs/cli.md).

## Exit codes

| Code | Meaning |
|-----:|---------|
| 0 | ok |
| 1 | not found, or an empty result (`ready` and `query` with no rows) |
| 2 | usage error |
| 3 | refused: validation, synced path, shared database, missing `LEDGER_HARNESS`, capacity, a failed quote |
| 4 | integrity failure (`verify` found a broken chain) |
| 5 | stale revision: `--expect-revision` no longer matches the item's newest event |

Errors print one line on stderr, or with `--json` an envelope whose `error`
holds a stable `code` (`missing_harness`, `validation`, `shared_database`,
`stale_revision`, ...) and a `message`. A user error never prints a stack
trace.

## Configuration

| Setting | Where | Meaning |
|---------|-------|---------|
| `--db` / `LEDGER_DB` | flag / env | the database path (required; the ledger picks no default) |
| `LEDGER_HARNESS` | env | what is writing: a tool, a script, an agent runtime. Required for every write |
| `LEDGER_OPERATOR`, `LEDGER_AGENT` | env | who is writing; the actor is stamped `<operator>/<agent>` when both are set |
| `LEDGER_MODEL`, `LEDGER_SESSION` | env | optional attribution stamped on each event |
| `LEDGER_EXPORT_DIR` | env | default for `export --dir` and `restore --dir` |
| `LEDGER_POINTER_SIDECAR` | env | an executable that returns conversation pointers for a card |
| `search_roots` | `config.json` beside the database | absolute paths whose `standards/*.md` and `decisions/*.md` the search index includes |
| `claim_ttl_seconds` | `config.json` beside the database | default claim lease (4 hours when unset) |

A good home for a database is `~/.local/share/ledger/<system>.db`. Keep it
out of git working trees: `index` and `search` refuse to build `search.db`
inside one, so that the derived index is never committed.

## Migrations

The schema lives in `ledger/migrations/NNNN_name.sql`, applied in version
order and recorded in a `schema_version` table. Migrations are forward only
and change views and indexes; they never rewrite or delete an event, so the
hash chain is the same before and after. A database that records a version
this copy of the ledger does not ship was written by a newer ledger, and
every open path refuses it rather than guess. Details and the list of
migrations: [docs/migrations.md](docs/migrations.md).

## Development

```sh
python3 -m pip install pytest
python3 -m pytest -q
python3 -m ledger.gen_verb_registry --check   # the static verb list is current
```

The package's own tests live in `ledger/tests/`; `tests/` holds the tests
for this repository's packaging. `ledger/` is also vendored, byte for byte,
into other repositories; [VENDORING.md](VENDORING.md) describes the mapping
and the drift check.

## License

MIT. See [LICENSE](LICENSE).
