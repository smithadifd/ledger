# CLI reference

```text
ledger [--db PATH] [--json] <verb> [verb flags]
python3 -m ledger [--db PATH] [--json] <verb> [verb flags]
```

`--db` and `--json` may appear before or after the verb. `--db` overrides
`LEDGER_DB`; one of them is required. `ledger <verb> --help` is the
authoritative flag list; this page summarises it.

## Output

Without `--json`, a verb prints short tab-separated lines (or a card) on
stdout and errors as one line on stderr. With `--json`, it prints exactly one
JSON object on stdout:

```json
{"data": {...}, "error": null, "ok": true}
{"data": null, "error": {"code": "missing_harness", "message": "LEDGER_HARNESS is required for writes"}, "ok": false}
```

## Exit codes

| Code | Name | Meaning |
|-----:|------|---------|
| 0 | ok | success |
| 1 | not found | no such item or database, or an empty listing |
| 2 | usage | bad flags or arguments |
| 3 | refused | validation, synced path, shared database, missing `LEDGER_HARNESS`, capacity, a quote that does not match, export lag |
| 4 | integrity | `verify` found a broken hash chain |
| 5 | stale | `--expect-revision` no longer matches the item's newest event |

Error codes in the JSON envelope include `usage`, `missing_harness`,
`missing_db`, `not_found`, `empty`, `validation`, `conflict`, `capacity`,
`shared_database`, `synced_path`, `stale_revision`, `integrity`, `unusable`,
`quote_mismatch`, `lag`, `verb_collision` and `invalid_actor`. A user error
never prints a stack trace.

## Environment

| Variable | Meaning |
|----------|---------|
| `LEDGER_DB` | database path when `--db` is not given |
| `LEDGER_HARNESS` | what is writing (a shell, a scheduled job, an agent runtime); every write verb refuses without it |
| `LEDGER_OPERATOR` | the person on whose behalf the write happens |
| `LEDGER_AGENT` | the process writing; the actor is `<operator>/<agent>` when both are set |
| `LEDGER_MODEL`, `LEDGER_SESSION` | optional attribution stamped on each event |
| `LEDGER_EXPORT_DIR` | default for `export --dir` and `restore --dir` |
| `LEDGER_POINTER_SIDECAR` | an executable the card runs with the item id; it prints a JSON array of `{"ref", "note"}` objects shown as conversation pointers |

## Configuration file

An optional `config.json` beside the database:

```json
{
  "search_roots": ["/absolute/path/to/a/notes/repo"],
  "claim_ttl_seconds": 14400
}
```

- `search_roots`: absolute paths. Under each, `standards/*.md` and
  `decisions/*.md` are indexed, plus any markdown file an item or decision
  names. Without the key, only the ledger itself is indexed.
- `claim_ttl_seconds`: the default claim lease. A malformed value makes
  `claim` refuse unless `--ttl` is given.

## Item verbs

**`new --type TYPE --title TITLE [--id ID] [--status STATUS] [--source SOURCE]`**
Create an item. Types: `topic`, `ticket`, `initiative`, `decision`, `gate`,
`guidance`. Without `--id` an id like `it-1a2b3c4d5e6f` is generated. With
`--source` the item is keyed by its source: repeating `new` with the same
source returns the same item, and a different id or title for that source is
refused, naming the existing item. A source of the form
`path:line@commit` lets `export-item --ticket` read the item's text back from
git.

**`note ITEM --text TEXT [--expect-revision ID]`** Append a note. Notes that
start with `work:` or `loop:` are reserved.

**`state ITEM --status STATUS [--owner O] [--claim C] [--blocked-by X] [--expect-revision ID]`**
Set the item's status.

**`claim ITEM [--owner O] [--claim C] [--ttl SECONDS] [--capacity N]`**
Claim the item with a lease. The holder defaults to the actor. `--capacity`
refuses the claim when N items are already claimed.

**`link ITEM --rel REL --target TARGET`** Link the item. `REL` is one of
`pr`, `branch`, `session`, `item`, `url`, `blocks`, `duplicate-of`.
`duplicate-of` also closes the item, in the same transaction, as a duplicate
of `TARGET`; both cards show the relation. There is no merge verb.

**`spec set|show|diff ITEM ...`** An item's spec is its versioned content:
`--title`, `--body` or `--body-file`, `--acceptance`, `--size` (`S`, `M`,
`L`) and `--merge-class` (`MC-0` to `MC-4`, a risk class the ledger stores
but does not interpret). `set` appends version N+1 only when the content hash
changes; the same content again reports `unchanged`. `show [--version N]`
prints a version; `diff ITEM [FROM [TO]]` prints the change field by field.
A claim stamps the spec version it was made against, and the card shows
`spec moved: vN → vM` when the spec changed under a claim.

## Entity verbs

**`entity register TYPE ID --title TITLE`**
**`entity set TYPE ID [--schedule S] [--wrapper W] [--host H] [--risk-class R] [--status S]`**
Keep a registry of automations, projects, machines and models as ledger
state, keyed by type and a stable id. `register` adds an entity; registering
a key that exists is refused. `set` appends only the fields whose value
changed, and when none did it writes nothing and reports `unchanged`; a state
field the schema does not name is refused. `entity` is the only writer of the
reserved `entity.registered` and `entity.state` kinds (`append` and
`append-many` refuse them) and needs `LEDGER_HARNESS`.

**`entities [--type TYPE] [--format md|json]`** List the registry from
`v_entities`. `--format json` prints a list of
`{type, id, title, state, revision}`, where `revision` is the entity's newest
event id: the shape a drift check compares against a live registry.
`--format md`, the default, prints one table per type under a line naming the
ledger revision (the newest event id) it was rendered from.

## Event verbs

**`append --kind KIND --payload JSON [--item ID] [--idempotency-key K] [--channel C] [--provenance-class P] [--payload-version N] [--expect-revision ID]`**
Append one validated event of any kind that is not reserved (`loop.*`,
`ledger.system` and `item.spec` are reserved). Retrying with the same key and
content returns the stored event.

**`append-many ITEM [--expect-revision ID]`** Read a JSON array of
`{kind, payload, ...}` objects from stdin and append them for one item in
one transaction: all land or none do.

## Decision verbs

**`elect ITEM --question Q --chosen LABEL --chosen-description D --rejected LABEL... --scope S --quote-ref EVENT --quote SPAN... [--ask EVENT]`**
Record an election. Every `--quote` must be a byte-exact substring of the
text of the `utterance.captured` event named by `--quote-ref`. `--ask`
closes exactly that open question.

**`pick ITEM --question Q --option LABEL[::DESCRIPTION]... --chosen LABEL [--free-text T] --scope S [--ask EVENT] [--idempotency-key K]`**
Record a question-picker answer: an utterance holding the question, the
chosen label and any typed text, plus an election quoting it. A chosen label
outside the offered options is recorded as a typed answer.

**`lint-quotes [--item ITEM]`** Re-check every election's quotes.

**`decisions [--item ITEM] [--tag TYPE:ID] [--standing lean|settled|superseded] [--limit N]`**
List decision records with their current standing.

## Reading verbs

**`query [--item ID] [--type TYPE] [--status STATUS] [--limit N]`** Current
items, at most 50 unless `--limit` says otherwise. Exit 1 when nothing
matches.

**`ready [--limit N]`** Open items with no open blocker, including claimed
items whose lease expired.

**`card ITEM [--md PATH]`** A bounded resume card: header (revision and
source event ids), fields, decisions, open questions with their options, the
next step, links, blockers, notes and resume sections. `--md` also writes a
markdown copy.

**`context ITEM [--budget N]`** A read-only orientation pack for an item or
an initiative (an item of type `initiative`; its members link to it with
`link --rel item`). The header names the item, its revision as the card names
it and the line `orientation only; not authority`. Then come the current spec,
decisions with their standing, open questions, linked documents, the last five
notes, and the items linked to it either way (depth 1) and to those (depth
2). Every content line starts `event=<id>`. `--budget` counts words (default
600). The header and spec always print; a section that does not fit collapses
to `## <section>: N more`, and every section after it then prints only its
floor, so depth-2 items go first. An unknown id exits 1, as `card` does. The
card stays the per-item view.

**`grants [--check GRANT_ID]`** List trust grants from election records. A
`decision.elected` whose `question_id` starts `grant:`, `countersign:` or
`rung:` is a grant, keyed by that id across items; the newest election per id
decides. `granted` is `STANDING` when it verifies and `UNVERIFIED` when it
does not; any other `chosen` (`withdrawn`) is `WITHDRAWN` without verifying.
Verified means the `quote_ref` is an `utterance.captured` a person wrote
(`submitted-prompt` or `tool-answer`) and every quote is a byte-exact
substring of it. The table prints one row per grant, sorted by grant id,
under a line naming the ledger revision; `--json` prints the same rows.
`--check` exits 0 when the grant is `STANDING`, else 3 with `grant_withdrawn`,
`grant_unverified` or `grant_unknown`. It reports; no gate reads it.

**`export-item ITEM [--ticket]`** One item's events, oldest first, without
ephemeral events recorded before its latest close. `--ticket` instead prints
the `### Ticket:` section its source locator points at, read with `git show`
from the current repository.

**`search QUERY [--limit N] [--type T] [--status S] [--item ID] [--initiative ID] [--since DATE] [--until DATE]`**
Full-text search, ranked by BM25. Types: `item`, `decision`, `utterance`,
`standard`, `adr`, `doc`. Rebuilds the index first when it is missing or
stale.

**`index [--rebuild]`** Build `search.db` beside the database.

## Integrity and durability verbs

**`doctor`** Print the resolved database path and schema version, the search
index's state (`missing`, `stale`, `current`) and any expired claims. Exits
1 when the database does not exist and 3 when it is unusable; the index and
claim reports never change the exit code.

**`verify [--system NAME]`** Walk the hash chain and re-validate every
payload. With `--system`, also require the database's claim to be `NAME`.

**`export [--dir DIR] [--commit | --push | --check [--max-events N] [--max-hours H]]`**
Write events after the watermark as canonical JSON files. `--commit` makes
one git commit in `DIR` and never pushes; `--push` pushes; `--check` reports
the lag and exits 3 when it is over the limits (100 events, 24 hours).

**`backup --to FILE`** Online backup with `PRAGMA integrity_check`.

**`restore --dir DIR --system NAME [--accept-loop-events]`** Replay an
export into the new database named by `--db`, then verify it. The target
must not exist.

## Guidance

**`guidance set|list|withdraw [--pool POOL] [--direction burn|protect] [--reason R] [--starts DATE] [--expires DATE]`**
Guidance is a dated note that a named resource pool (a usage quota, a
budget) should be spent down (`burn`) or held back (`protect`), and why. One
item stands for one pool and direction; setting it again appends a new
version and the latest wins; `withdraw` closes it early. Guidance is advice
for whatever reads it: the ledger records it and enforces nothing.
