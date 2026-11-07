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

**`note ITEM TEXT [--expect-revision ID]`** Append a note. The text is
given once, after the item or as `--text TEXT`, never both. Notes that
start with `work:` or `loop:` are reserved.

```sh
ledger note it-1a2b3c4d5e6f "tried the retry; it worked"
```

**`note ... [--idempotency-key K]`**, **`link ... [--idempotency-key K]`** and
**`elect ... [--idempotency-key K]`** take the same retry key `append` does.

**`state ITEM --status STATUS [--owner O] [--claim C] [--blocked-by X] [--expect-revision ID]`**
Set the item's status.

**`claim ITEM [--owner O] [--claim C] [--ttl SECONDS] [--capacity N]`**
Claim the item with a lease. The holder defaults to the actor. `--capacity`
refuses the claim when N items are already claimed.

**`link ITEM TARGET [--rel REL]`** Link the item. The target is given once,
after the item or as `--target TARGET`, and may be a local file path. `REL`
defaults to `url` and is one of `pr`, `branch`, `session`, `item`, `url`,
`blocks`, `duplicate-of`, `child-of`; it goes after `--rel`, never as a
bare word.
`duplicate-of` also closes the item, in the same transaction, as a duplicate
of `TARGET`; both cards show the relation. There is no merge verb.
`child-of` makes the item a child of the anchor `TARGET`. An item has one
anchor at a time: a second `child-of` to a different anchor is refused
(exit 3, naming the current anchor) unless `--move` is passed, which records
a new link that names the anchor it left. The same link again writes
nothing, but with `--idempotency-key` it is refused (code `key_on_noop`),
because a key must name a stored event for a retry to replay. A target that
is not an item is refused. A link that would make the item its own ancestor is refused and
writes nothing. `--move` is accepted only with `--rel child-of`. The
same rules hold for a `child-of` link written by `append` or `append-many`
or replayed by `restore`: a link to a different anchor must name the current
one in `moved_from`, and a self-link or a cycle is refused (exit 3, codes
`self_link`, `second_anchor`, `anchor_missing`, `cycle`; a refused `append-many` batch writes
none of its events). A key already used by another event is refused as
`conflict`, as on every link. The anchor's card gets a Children section
headed `closed/total` with one line per child (control characters in a title show as escapes), and `resume` prints the same count for an anchor.
When a long card cuts the section, its marker names `ledger query
--parent=ANCHOR --all`, which lists every child the anchor has when you run it.
`--idempotency-key K` makes a retry safe (see Retrying with a key below).

**`spec set|show|diff ITEM ...`** An item's spec is its versioned content:
`--title`, `--body` or `--body-file`, `--acceptance`, `--size` (`S`, `M`,
`L`) and `--merge-class` (`MC-0` to `MC-4`, a risk class the ledger stores
but does not interpret). `set` appends version N+1 only when the content hash
changes or `--source` differs from the current version's source; the same
content with the same source (or with no `--source`) again reports
`unchanged`. `--source` records where the item's current spec lives, so moving
it over the same content writes a new version with the same content and the new
source, and moving it back is another change. `--source-rev` is not compared. `show [--version N]`
prints a version; `diff ITEM [FROM [TO]]` prints the change field by field.
A claim stamps the spec version it was made against, and the card shows
`spec moved: vN → vM` when the spec's content changed under a claim. A version
written only to move the source leaves the content hash as it was and prints
nothing.

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

**Retrying with a key.** `append`, `pick`, `note`, `link` and `elect` take
`--idempotency-key K`. Without it every call writes a fresh event. With it:
no earlier event under `K` means the call writes; an earlier event with the
same content is returned (exit 0, the first event's id) and nothing is
written; an earlier event with different content is refused as a `conflict`
(exit 3). `note`, `elect` and a plain `link` write one event under `K` itself.
`link --rel duplicate-of` writes two events in one transaction, under
`K` (the link, the same slot a plain `link` uses) and `K:close` (the close), so
a retry returns both and writes none, and a plain `link` and a `duplicate-of`
under one `K` refuse as a `conflict`. If only one of the two exists (it was
written by some other call), the call is refused as `partial_write` and writes
nothing; use a different key.

**`append-many ITEM [--expect-revision ID]`** Read a JSON array of
`{kind, payload, ...}` objects from stdin and append them for one item in
one transaction: all land or none do.

## Decision verbs

**`elect ITEM --question Q --chosen LABEL --chosen-description D --rejected LABEL... --scope S --quote-ref EVENT --quote SPAN... [--ask EVENT]`**
Record an election. Every flag but `--ask` is required, and `--rejected`
and `--quote` are repeatable: give `--rejected` once for each alternative that
was not chosen. Every `--quote` must be a byte-exact substring of the
text of the `utterance.captured` event named by `--quote-ref`. `--ask`
closes exactly that open question. `ledger elect --help` has a worked
example.

**`pick ITEM --question Q --option LABEL[::DESCRIPTION]... --chosen LABEL [--free-text T] --scope S [--ask EVENT] [--idempotency-key K]`**
Record a question-picker answer: an utterance holding the question, the
chosen label and any typed text, plus an election quoting it. A chosen label
outside the offered options is recorded as a typed answer.

**`lint-quotes [--item ITEM]`** Re-check every election's quotes.

**`decisions [--item ITEM] [--tag TYPE:ID] [--standing lean|settled|superseded] [--limit N]`**
List decision records with their current standing.

## Reading verbs

**`query [--item ID] [--type TYPE] [--status STATUS] [--parent ID] [--since DATE] [--until DATE] [--limit N | --all]`**
Current items, at most 50 unless `--limit` says otherwise; `--all` removes the cap
(with `--limit` it is a usage error, and `limit` in the JSON is then `null`). `--parent ID`
keeps the children of `ID` (items whose current `child-of` anchor is `ID`). `--since` and
`--until` keep the items whose newest event falls in the window, with the
same date forms and the same refusals as `search`. Exit 1 when nothing
matches. Each row in `items` carries the item's fields plus `revision`, `parent_id`
(the item's current `child-of` anchor as a string id, or `null`) and `children_open` (how
many of its children are not closed, an integer of 0 or more). The text output lists
id, type, status and title only.

**`events [--since DATE] [--until DATE] [--kind KIND] [--item ID] [--actor ACTOR] [--limit N]`**
Events across all items, newest first, at most 50 unless `--limit` says
otherwise (1 to 1000). `--since` and `--until` filter on each event's
recorded time with the same date forms and the same refusals as `query`.
`--kind` may be repeated (any of the given kinds). Filters combine with AND.
Exit 1 when nothing matches. `--json` carries
`{events, limit, truncated}`; each event is
`{id, kind, item_id, actor, harness, model, session, recorded_at, payload}`.

```sh
ledger events --kind item.note --limit 20
```

**`questions [--item ID] [--type TYPE] [--since DATE] [--until DATE] [--limit N]`**
Open asks across all items, oldest first. `--type` is the item's type.
`--since` / `--until` filter on the ask's recorded time with the same date
forms as `query`. Exit 1 when nothing matches. `--json`
carries `{questions, limit, truncated}` with each ask joined to the item's
current type, title and status.

```sh
ledger questions --type ticket
```

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
ephemeral events recorded before its latest close, then one completeness
line: `completeness: N events returned, M events on the item, K decayed`
(with `--json`, a `completeness` object in `data` with `events_returned`,
`events_on_item` and `events_decayed`). `--ticket` instead prints
the `### Ticket:` section its source locator points at, read with `git show`
from the current repository.

**`search WORD... [--limit N] [--type T] [--status S] [--item ID] [--initiative ID] [--since DATE] [--until DATE]`**
Full-text search, ranked by BM25; the words may be quoted or separate.
Types: `item`, `decision`, `utterance`, `standard`, `adr`, `doc`. Rebuilds
the index first when it is missing, stale or behind new events, and refuses by name (exit 3)
when it cannot rewrite the index, rather than answering from it.

**`index [--rebuild]`** Build `search.db` beside the database.

## Integrity and durability verbs

**`doctor`** Print the resolved database path and schema version, the search
index's state (`missing`, `stale`, `behind`, `current`) and any expired claims. A
write after the last index build is not stale: doctor reports `current to
event N, M new events will be indexed on the next search` (JSON status
`behind`, with `indexed_to_event` and `new_events`), and `search` rebuilds
before it answers. `stale` is kept for a real mismatch: a different format,
ranker, database or roots, an added, edited or deleted source file, or an
event log that no longer matches what was indexed. Exits
1 when the database does not exist and 3 when it is unusable; the index and
claim reports never change the exit code. `--json` also carries a `summary`
object from one snapshot: `items_by_type`, `items_by_status`,
`events_total`, `last_event_at` (null when the log is empty),
`open_questions`, `leases` (claimed items, including expired ones, which `expired_claims` lists), and `export` as `{watermark_event_id, behind}`
when `LEDGER_EXPORT_DIR` is set. `behind` counts events with id greater than
the watermark. There is no `last_export_at` (the watermark sidecar records
only an event id) and no `verify` field (verify runs are not recorded). A
failure reading the summary or the watermark becomes
`{"status": "unknown", "reason": "..."}` for that part and never changes the
exit code. Without `LEDGER_EXPORT_DIR`, `export` is omitted.

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

## Resume

**`resume [--item ID] [--brief]`**
For each open item that has an `item.stopped` event, print the newest stop
(its point, the skills to invoke next, the files to read) and the commands
that continue it: `ledger --db PATH resume --item ID` and
`ledger --db PATH card ID`. With `--item`, show only that item (exit 1 when
it does not exist). `--brief` first prints the short form of the agent
contract ([agent-contract.md](agent-contract.md)), so a session-start hook can
print both. It never writes, and a database that does not exist yet prints
`no open items` and exits 0.
