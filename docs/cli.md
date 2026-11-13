# CLI reference

```text
ledger [--db PATH] [--json] <verb> [verb flags]
python3 -m ledger [--db PATH] [--json] <verb> [verb flags]
ledger --version
```

`--db` and `--json` may appear before or after the verb. `--db` overrides
`LEDGER_DB`; one of them is required, because the ledger has no default
database path. The supported location is
`~/.local/share/ledger/<system>.db` (or whatever `LEDGER_DB` names).
`ledger <verb> --help` is the
authoritative flag list; this page summarises it. `--version` needs no
verb or database and prints `ledger <version>`.

## Output

Without `--json`, a verb prints short tab-separated lines (or a card) on
stdout and errors as one line on stderr. With `--json`, it prints exactly one
JSON object on stdout:

```json
{"data": {...}, "error": null, "ok": true}
{"data": null, "error": {"code": "missing_harness", "message": "LEDGER_HARNESS is required for writes; set it, e.g. LEDGER_HARNESS=claude-code"}, "ok": false}
```

## Exit codes

| Code | Name | Meaning |
|-----:|------|---------|
| 0 | ok | success |
| 1 | not found | no such item or database, or an empty listing |
| 2 | usage | bad flags or arguments |
| 3 | refused | validation, synced path, shared database, missing `LEDGER_HARNESS`, capacity, a quote that does not match, export lag, an export, commit or push refusal |
| 4 | integrity | `verify` found a broken hash chain or a broken child-of forest; `doc verify` or `doc show --content` found a document whose bytes do not hash to its sha256 and size, or a by-path document whose file is missing or refused |
| 5 | stale | `--expect-revision` no longer matches the item's newest event |

Error codes in the JSON envelope include `usage`, `missing_harness`,
`missing_db`, `not_found`, `empty`, `validation`, `conflict`, `capacity`,
`shared_database`, `synced_path`, `stale_revision`, `integrity`, `unusable`,
`quote_mismatch`, `lag`, `verb_collision`, `invalid_actor`, `doc_unreadable`,
`doc_outside_root` (exit 3), `export_doc_credential`, `export_head_detached`, `export_push_unborn`, `export_push_unconfigured`, `export_push_multiple_urls`, `export_repo_shallow`, `export_history_incomplete`, `export_file_credential`, `export_event_invalid`, `export_object_unreadable`, `export_head_moved`, `export_commit_ancestry`, `export_branch_credential`, `export_commit_sign_failed`, `export_git_failed`, `export_push_failed` (each exit 3), `doc_credential_shape`,
`restore_doc_credential`, `doc_mismatch`, `doc_missing`, `doc_refused` (each
exit 4) and `doc_verify_failed`. A user error never
prints a stack trace.

## Environment

| Variable | Meaning |
|----------|---------|
| `LEDGER_DB` | database path when `--db` is not given; there is no default, and the supported location is `~/.local/share/ledger/<system>.db` |
| `LEDGER_HARNESS` | what is writing (a shell, a scheduled job, an agent runtime); every write verb refuses without it and suggests `LEDGER_HARNESS=claude-code` |
| `LEDGER_PROG` | command prefix printed by `resume` and `card`; otherwise a module run uses `python3 -m ledger` and other runs use the basename of `argv[0]` |
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
  "claim_ttl_seconds": 14400,
  "capture_prompts": true
}
```

- `search_roots`: absolute paths. Under each, `standards/*.md` and
  `decisions/*.md` are indexed, plus any markdown file an item or decision
  names. Without the key, only the ledger itself is indexed.
- `claim_ttl_seconds`: the default claim lease. A malformed value makes
  `claim` refuse unless `--ttl` is given.
- `capture_prompts`: `false` turns prompt capture off: `capture --kind prompt`
  writes nothing and prints `skipped` with reason `prompts_off`. Picker
  capture (`capture --kind picker`) never reads this key, so it stays on. No
  file, no key, or `true` leaves prompt capture on. A value that is not a JSON
  boolean (`"false"`, `0` and `null` included), or a `config.json` that cannot
  be read or is not a JSON object, makes `capture --kind prompt` refuse with
  `capture_config` (exit 3) and write nothing, so a typo never turns a `false`
  into stored prompts; picker capture still works. Prompt capture decides
  with one `lstat`: no entry named `config.json` means no file; any entry (a
  file, a link, anything else) is opened and read once, and any failure to
  open, read or parse it refuses `capture_config` with the cause it saw. A
  link whose target is missing, loops or vanishes before the open counts as
  unreadable here, though `search` and `claim` read such a link as no file.

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

**`claim-system NAME`** Claim this database for a system, for example `work` or
`home`. A system name is 1 to 64 characters: a lowercase letter, then lowercase
letters, digits, `_` or `-`; anything else is refused with exit 2 before the
database is opened. An unclaimed database is claimed and the verb exits 0. The
same name again exits 0, says the database is already claimed for that name and
writes nothing (a read-only database file refuses even the repeat, as every
write verb does). A different name is refused (exit 3, code `shared_database`,
naming both systems) and writes nothing. The verb reports the outcome its
transaction reached: two callers claiming the same name at once both exit 0 and
exactly one says `claimed`. After an I/O error in the claim's transaction
(before the write or after it) or a checkpoint that cannot finish because
another connection still holds the file, the verb reads the claim back and
retries the checkpoint, up to 5 attempts started within 1 second: it exits 0
only when the name's claim is on record and a checkpoint finishes, the same
proof as a claim with no error (`claimed` only when this call's write is known
to have committed it). Anything else, including a claim on record whose
checkpoint is still held when that budget is spent, or a read-back or
checkpoint that fails, is refused (exit 3, code `shared_database`). A synced
or linked database path is
refused as every write verb refuses it (exit 3, codes `synced_path` and
`db_symlink`), and no file is created. `--json` prints `system`, `path` and
`claimed` (`false` for the repeat). Like every write verb it needs
`LEDGER_HARNESS`. The claim is one event inside the database file, so any path
that reaches the file reaches the claim, and an export or restore carries it;
`verify --system NAME` and `restore --system NAME` check it.

```sh
ledger claim-system work
```

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
`set ... --expect-revision ID` writes only while the item's newest event is
still `ID`, with the values and refusals `note` takes. The check runs in the
same write transaction as the compare, after the not-found check (a missing
item still exits 1) and before the unchanged compare: a stale value exits 5
(`stale_revision`) and writes nothing, even when the content and source are
unchanged. `none` never matches, since every item has its creation event.
A claim stamps the spec version it was made against, and the card shows
`spec moved: vN → vM` when the spec's content changed under a claim. A version
written only to move the source leaves the content hash as it was and prints
nothing.

**`doc add ITEM FILE --role ROLE [--by-path] [--title T] [--mime M] [--allow-credential-shapes]`**
Attach a document to an item as one `doc.attached` event. `ROLE` is one of
`review-receipt`, `brief`, `packet`, `retro`, `other`. The ledger reads
`FILE`'s bytes once and computes their sha256 and size itself. By default it
stores the bytes inline (base64) in the event, and a `FILE` over the
65536-byte inline limit is a usage error (exit 2) that names `--by-path`.
With `--by-path` it stores `FILE`'s path under the document root instead (see
below), for a file of at most 16777216 bytes (16 MiB; over it is a usage
error, exit 2). The media type defaults to `application/octet-stream`, and
to `text/markdown` for a `.md` file. Adding the same item, role and sha256
again writes nothing and reports `unchanged`, whatever the storage: adding by
path the bytes an inline document already holds returns the inline document,
and a second path to the same bytes returns the first. The same bytes under
another role are a second document. An unknown item is `not_found` (exit 1).
`ledger append` and `append-many` refuse the `doc.attached` kind. `doc add`
is a write and needs `LEDGER_HARNESS`; `doc show` and `doc verify` are reads
and do not.

`doc add` opens `FILE` one component at a time, as given, from `/` (or
from the current directory for a relative `FILE`), each open relative to the
directory opened before it and none following a symlink. A symlink at any
component not yet opened, the file itself or a directory above it, is
refused `doc_unreadable` (exit 3) rather than followed, including one
swapped in while the walk runs. A directory replaced after the walk opened
it does not redirect the walk, because later opens are relative to the
descriptor already held. A `..` component is refused, never normalised; a
repeated `/` and a `.` component are skipped. On macOS `/tmp`, `/var` and
`/etc` are symlinks into `/private`, so pass the real path (for example
`/private/tmp/brief.md`). A `FILE` that is empty, ends in `/`, or is not a
readable regular file (missing, a directory, a FIFO) is `doc_unreadable` as
well, and so is every `FILE` on a platform that cannot open a path without
following symlinks. A filename in a refusal is escaped as `events` escapes a
text column.

A by-path document names a file under the database's *document root*: the
directory beside the database file named as the database file plus
`.documents` (for `--db ~/.local/share/ledger/work.db`, the root is
`~/.local/share/ledger/work.db.documents`). Each database has its own, and the
database, its sidecars, `config.json` and the search index stay outside it.
The ledger never creates, writes, moves or deletes anything under the root:
put the file there first. The event stores the path relative to the root
(`path`), never an absolute path and never the bytes. `--by-path` places
`FILE` under the root component by component, never by string prefix (so
`work.db.documents2` is not under `work.db.documents`), and character for
character (on a case-insensitive volume a `FILE` spelled in another case than
the root is refused). An absolute `FILE` must start with the root in one of
two spellings: the physical root (the database's directory with symlinks
resolved), or the root as the database path was spelled (`~` expanded and
made absolute, not resolved). The second spelling is for macOS, where `/tmp`,
`/var` and `/etc` are symlinks: a database named `/tmp/x/work.db` takes
`FILE` `/tmp/x/work.db.documents/a.md`. A relative `FILE` is read from the
working directory as the operating system reports it, which is physical, so
from the database's directory `work.db.documents/a.md` works. Anything else
is refused `doc_outside_root` (exit 3), naming `FILE` and the root, and
nothing is written. `FILE` naming the root itself, and a stored path over
1024 bytes, with a component over 255 bytes or holding a control character,
is `doc_unreadable`; `..` is refused and a repeated `/` and a `.` component
are dropped, as for an inline add. `doc add --by-path` opens the root from
the database's directory without following a symlink, walks the stored path
from it as above, and reads the bytes from that walk; a relative `FILE` is
also walked from the working directory, and refused `doc_unreadable` unless
both walks reach the same file. A symlink at the root or anywhere below it is
refused. Each component below the root must also appear byte for byte in its
directory's entry list, read from that directory's descriptor: on a
case-insensitive volume a name spelled otherwise than on disk would open, and
store one file under a second spelling, so it is refused `doc_unreadable`
(its spelling differs from the name on disk) and never rewritten. The
credential scan below runs on the bytes read, as for an inline add, and on
the stored path.

`FILE` given as a single hyphen (`-`) reads standard input, up to the same
inline limit (over it is a usage error, exit 2); the media type defaults to
`application/octet-stream`. A file whose name is a single hyphen is passed
as `./-`. Standard input is a convenience for piping generated text into a
document. Standard input has no path to store, so `-` with `--by-path` is a
usage error (exit 2).

`doc add` attaches whatever bytes `FILE` holds and does not vouch for where
they came from. The ledger runs as the caller's operating-system user, so
any process that can run `ledger doc add` with `LEDGER_HARNESS` set (an
agent lane included) can attach any file that user can read: by its own
path, through a hard link, or through a copy. No check at `doc add` can tell
those apart, and none tries; the same holds for a file under the document
root, which any process running as that user can put there by copy or by
hard link. What the ledger does guarantee is that the stored sha256 is the
digest of the bytes it read, and that a document's bytes are never printed
or reported `ok` unless the bytes read in that call still match it.

Before anything is written, `doc add` scans the bytes for known credential
shapes: a private-key header (`-----BEGIN ... PRIVATE KEY-----`), a GitHub
token (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` or `github_pat_`), an AWS
access key id (`AKIA` or `ASIA`), an `sk-` API key, and a Slack token
(`xoxa-`, `xoxb-`, `xoxp-`, `xoxr-`, `xoxs-`). Every shape but the
private-key header matches only at the start of the bytes or after a byte
that is not an ASCII letter, digit or underscore, so `risk-free` is not an
`sk-` key. A hit is refused `doc_credential_shape` (exit 3), naming `FILE`
and each shape with its byte offset, never the matched bytes, and nothing is
written. With `--by-path` the UTF-8 bytes of the stored path are scanned
too, before any file is opened; a hit there is refused `doc_credential_shape`
naming each shape as in the path, and the refusal names neither `FILE` nor
the path. `--allow-credential-shapes` attaches it anyway and marks the event
`credential_override: true`; the result says so (`credential-override` in
text, `data.credential_override: true` with `--json`). The flag on bytes
and a path with no hit writes no marker. The scan runs before the `unchanged` check, so
repeating an overridden add without the flag is refused. The scan is a
tripwire for these common shapes, not a secret detector: a credential in any
other shape passes it.

**`doc show ITEM [--id N] [--content]`** List the item's documents, oldest
first: event id, role, size, the start of the sha256, the storage (`inline`,
or `path` followed by the stored path, escaped as a text column), the title,
and `credential-override` when the document carries the marker. `--id N`
shows one. With `--json`:

```json
{"data": {"documents": [{"credential_override": false, "event_id": 7, "item_id": "it-1", "mime": "text/markdown", "path": null, "role": "brief", "sha256": "<64 hex>", "size": 120, "storage": "inline", "title": "The brief"}], "item_id": "it-1"}, "error": null, "ok": true}
```

`storage` is `inline` or `path`; `path` is null for an inline document and
the stored path (relative to the document root) for a by-path one. No
documents is `documents: []` and exit 0. Without `--content`, `doc show`
reads only the ledger, never a file. `--content` (with `--id`) reads the
document's bytes and hashes them first: an inline document's stored bytes,
or for a by-path one the file its stored path reaches when walked from the
document root of the database `--db` names, on this call, without following
a symlink. When they match the stored sha256 and size, it writes exactly the
bytes it hashed to stdout with no trailing newline (never a second read), and
with `--json` it prints `{"document": {<the row>, "content_b64": ...}}`.
Otherwise nothing is printed and it is refused (exit 4): `doc_mismatch` when
the bytes do not match, `doc_missing` when the root, a directory or the file
under it is absent, and `doc_refused` for any other failure (a symlink at
the root or below it, a FIFO or other non-regular file, a platform that
cannot open a path without following symlinks).

**`doc verify [ITEM] [--id N]`** Check every document in scope (every
document in the ledger when no item is given) as `doc show --content` reads
it, and report each as `ok`, `mismatch`, `missing` or `refused`. An inline
document's stored bytes are re-hashed; a by-path document's file is walked
from the document root and hashed again on every call, so a file changed,
moved, or replaced by a symlink after the add is reported, never followed.
All `ok` prints `ok documents=N` and exits 0.
Otherwise it prints `doc verify failed: F of N documents` and one line per
failing document, and exits 4; with `--json` the envelope reads
`ok: false`, error code `doc_verify_failed`, and keeps every row in
`data.documents` (`event_id`, `item_id`, `sha256`, `status`).

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
content returns the stored event. A `--channel` that starts with `hook:` is
reserved for `capture` and refused (`reserved-capture-channel`), here and in
`append-many` and the library; a `picker` field in an `utterance.captured`
payload is refused on any channel but `hook:picker`.

**Retrying with a key.** `append`, `pick`, `note`, `link` and `elect` take
`--idempotency-key K`. Without it every call writes a fresh event. With it:
no earlier event under `K` means the call writes; an earlier event with the
same content is returned (exit 0, the first event's id) and nothing is
written; an earlier event with different content is refused as a `conflict`
(exit 3). `note`, `elect`, `pick` and a plain `link` write one event under `K`
itself.
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
example. An election `elect` writes is evidence: no gate opens on it (see
"Answers that open a gate" in [concepts.md](concepts.md)).

**`ask-display EVENT`** Print the AskUserQuestion input for the ask: a
`questions` array with one question, `[ask K] <question> (scope: <scope>)`
(`[ask K] <question> (scope: <scope>; question id: <question_id>)` when the
ask has a question id, so a grant id is on what the owner sees),
header `ask K`, the ask's options in order as `{label, description}` (a
missing description shows the label) and `multiSelect` false, then a line
`display_sha256 <hex>`, the SHA-256 of that array's canonical JSON. `--json`
carries `{asked_event_id, item_id, input, display_sha256}`. An ask with no
`scope`, with fewer than two or more than four options, with a repeated
label, or whose header would pass 12 characters is refused with
`ask_not_pickable` (exit 3); an id that is no ask exits 1 (`ask_not_found`).

**`pick ITEM --ask EVENT [--idempotency-key K]`** Record the owner's picker
answer to an open ask as an election. The answer is the earliest
`hook:picker` capture tagged for the ask; the election takes the capture's
fingerprint and option position and every other field from the ask at that
position, and is checked as a gate would check it before it is written.
Refusals (exit 3): `unresolvable_ask`, `capture_missing`, and the reasons of
"Answers that open a gate" in [concepts.md](concepts.md), such as
`capture_conflict`, `display_differs`, `picker_typed` and `picker_annotated`.
The old `--question/--option/--chosen` form is refused with `pick_needs_ask`;
any of those flags or `--scope` beside `--ask` is a usage error.

**`capture --kind prompt|picker`** The harness's hooks record what they saw;
an agent never runs this. Stdin is one hook JSON object. `prompt` takes the
UserPromptSubmit input and writes the prompt on channel `hook:prompt`
(provenance `submitted-prompt`; a prompt over 64 KiB is stored as a pointer
with its size and SHA-256). `picker` takes the PostToolUse input for
AskUserQuestion and writes one event per call on channel `hook:picker`
(provenance `tool-answer`) whose `picker` object holds the displayed
`questions` array verbatim, its fingerprint, the answers map, any
annotations, the chosen option position of a one-question picker, and the
ask an `[ask K]` tag at the start of the first question names. Refusals
(exit 3): `capture_not_prompt`, `capture_not_picker`, `capture_bad_input`,
and `capture_config` (below).

Two cases of `--kind prompt` write nothing and exit 0. Both are decided after
the input is checked (so every refusal above still applies) and before the
database is opened (so neither creates it). Each prints `skipped`, a tab and
the reason; `--json` carries `{"event": null, "skipped": "<reason>"}`.

- `frame`: after its leading ASCII whitespace (space, tab, newline, carriage
  return, vertical tab, form feed), the prompt starts with one of the harness
  frames `<task-notification>`, `<cross-session-message`, `<agent-message`,
  `<system-reminder>` or `[Subagent hand-back]`: text the harness put in the
  prompt slot, not the person's. The match is exact and case-sensitive. The
  size rule comes after it, so a frame over 64 KiB is skipped too. The filter
  is a label, not proof of authorship: a prompt with no frame is stored with
  `origin: person` whoever wrote it, and a person who starts a prompt with a
  frame is not captured.
- `prompts_off`: `capture_prompts` is `false` in `config.json` (see
  [Configuration file](#configuration-file)). A `config.json` entry that
  cannot be opened, read or parsed (a link that does not resolve included) or
  holds a non-boolean value refuses `capture_config` instead.

Re-running one hook input: a picker input always carries `tool_use_id`, and
its key is `capture:picker:<tool_use_id>`; a prompt input that carries
`prompt_id` is keyed `capture:prompt:<prompt_id>`. For either, running it
twice leaves one capture: the retry returns the stored event, and a different
payload under the same key is refused (`conflict`). A prompt input without
`prompt_id` (older Claude Code) gets a random key, because a re-run and a
second identical submission look the same: a re-run may store a duplicate; it
never merges two submissions.

**`lint-quotes [--item ITEM]`** Re-check every election's quotes.

**`decisions [--item ITEM] [--tag TYPE:ID] [--standing lean|settled|superseded] [--limit N]`**
List decision records with their current standing.

## Reading verbs

**`query [--item ID] [--type TYPE] [--status STATUS] [--open] [--parent ID] [--duplicate-of ID] [--blocks ID] [--since DATE] [--until DATE] [--limit N | --all]`**
Current items, at most 50 unless `--limit` says otherwise; `--all` removes the cap
(with `--limit` it is a usage error, and `limit` in the JSON is then `null`). `--parent ID`
keeps the children of `ID` (items whose current `child-of` anchor is `ID`).
`--duplicate-of ID` keeps the other items with a `duplicate-of` link to `ID`
(the card's `duplicated-by` rows), and `--blocks ID` keeps the items with a
`blocks` link to `ID`, whatever their status. `--open` keeps the items that are
not closed (`open`, `claimed` or `blocked`); with `--blocks ID` that is the set
of blockers the card counts, since the card leaves closed blockers out.
Without `--open` the output is unchanged. `--since` and
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
The text form is one event per line: recorded time, kind, item, actor and a
payload summary, tab-separated. An empty column prints `-`, and a tab, newline
or other non-printable character in a column prints escaped (`\t`, `\n`), so
an event never spans two lines.

**`events --id N [--id N ...]`** Exactly the events with those ids, in id
order (oldest first), each once however often it is named. `N` is a positive
integer; anything else is a usage error (exit 2). `--id` reads by id alone: with
`--since`, `--until`, `--kind`, `--item`, `--actor` or `--limit` it is a usage
error naming the flag. An id no event has is refused (`not_found`, exit 1,
`event not found: <ids>`, naming every missing id) and nothing is printed.
`--json` carries the same `{events, limit, truncated}` with `limit` `null` and
`truncated` `false`.

```sh
ledger events --kind item.note --limit 20
ledger events --id 42 --id 17
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

**`card ITEM [--md PATH] [--full]`** A bounded resume card: header (revision and
source event ids), fields, decisions, open questions with their options, the
next step, links, blockers, notes and resume sections. `--md` also writes a
markdown copy. `--full` prints the same card unbounded: every row, every word
of every row and every source event id, with no caps and no cut markers, so it
can run past the 400-word bound. Links show the newest 8 and a count of the
rest; past the word bound the oldest shown link goes first. A section a long
card cuts ends with `(+K cut; COMMAND)`, where
`COMMAND` prints the rows it cut: `ledger export-item --json -- ITEM` for rows
the item's own events hold, `ledger query --parent=ITEM --all` for children,
`ledger query --duplicate-of=ITEM --all` for `duplicated-by` links and
`ledger query --blocks=ITEM --open --all` for blockers named only by another
item's `blocks` link (`--open`, because the card leaves closed blockers out). A
section that cut both its own rows and other items' rows has one marker for
each. The next step names at most five blockers; its `(+K cut; COMMAND)` for
the rest names the same commands by the same kinds, so blockers held only by
other items' `blocks` links point at the `query --blocks` command, not at
`export-item`.

With `--json`, `truncated` lists the names of what the card cut and `cuts`
carries one entry for each marker the text form prints, in the same order:
`section`, `count` and `command` exactly as the text prints them, and `unit`
(`rows` for `(+K cut; ...)`, `words` for `(+K words cut; ...)`). An ask field's
entry also has `event_id`, the event holding its full text; the words cut
from asks the card then capped away have one `ask` entry without it. Every
name in `truncated` has at least one entry. A card that cut children and
blockers:

```json
{
  "truncated": ["children", "blockers"],
  "cuts": [
    {"section": "children", "count": 31, "unit": "rows",
     "command": "ledger query --parent=it-anchor --all"},
    {"section": "blockers", "count": 2, "unit": "rows",
     "command": "ledger export-item --json -- it-anchor"},
    {"section": "blockers", "count": 3, "unit": "rows",
     "command": "ledger query --blocks=it-anchor --open --all"}
  ]
}
```

**`links ITEM`** Every link that names the item, in both directions. Outgoing
rows are the item's own links (`link ITEM ...`); incoming rows are other items'
links whose target is `ITEM`, of any relation. Each list is in event order,
oldest first; a link an item holds to itself is in both. Each row names the
other end (an outgoing link's target, an incoming link's holder) and how it
resolves: `item` (an item; its title is shown), `unresolved` (it should be an
item, as for the `item`, `blocks`, `duplicate-of` and `child-of` relations and
every incoming holder, but no item has that id) or `external` (a `pr`,
`branch`, `session` or `url` target that is not an item id). An unresolved row
is printed, never dropped. The text form is one row per line:
`out|in`, relation, other end, resolution, title (`-` when none) and
`event=<id>`, tab-separated, outgoing rows first; a tab or newline in a column
prints escaped. `--json` carries `{item_id, title, outgoing, incoming}`; an
outgoing row is `{event_id, rel, target, resolution, title}` and an incoming
row `{event_id, rel, source, resolution, title}`, with `title` `null` unless
the end resolves. An unknown item exits 1 (`not_found`, `item not found: ID`),
as `card` does; an item no link names exits 1 with `empty`. It only reads.

```sh
ledger links it-1a2b3c4d5e6f
```

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
Verified means the election answers an ask whose `question_id` is the grant
id (else the reason is `grant_unasked`) and is that ask's bound answer: the
owner's picker answer, recorded with `pick --ask` (else the reason is the
gate's, or `not_first_answer`). The table prints one row per grant, sorted by grant id,
under a line naming the ledger revision; `--json` prints the same rows.
`--check` exits 0 when the grant is `STANDING`, else 3 with `grant_withdrawn`,
`grant_unverified` or `grant_unknown`. It reports; no gate reads it.

**`export-item ITEM [--ticket]`** One item's events, oldest first, without
ephemeral events recorded before its latest close, then one completeness
line: `completeness: N events returned, M events on the item, K decayed`
(with `--json`, a `completeness` object in `data` with `events_returned`,
`events_on_item` and `events_decayed`). `--ticket` instead prints
the ticket section its source locator points at, read with `git show`
from the current repository: a ticket heading through the line before the next
heading of level three or less. A ticket heading is a level-3 heading outside
any code fence, in one of two forms:

- `### Ticket: ID — title`, where one lead word may come before the id (a
  `[tag]` or a lowercase slug, as in `### Ticket: [tag] ID — title`);
- a bare `### ID — title` inside a `## Tickets` section (the nearest heading of
  level two or less above it is exactly `## Tickets`); a bare id needs a digit
  and takes no lead word.

The title is optional and follows `: ` or a spaced em dash, en dash or hyphen.
An id is uppercase-led segments joined by single hyphens, each of which may end
in one lowercase letter (`SL12`, `SL12a`, `CSF3-9-H1`); an `RM-` id is never a
ticket id. This is the roadmap importer's id grammar, so a ticket the importer
reads is one this view shows, and an id it refuses is refused here too. A
legacy heading whose id is lowercase or holds an underscore carries no id:
`### Ticket: sl40 — title` and `### Ticket: SL_41 — title` are ticket
headings with no id (the importer reports each as `no stable id` and imports
nothing), and the bare `### sl40 — title` and `### SL_41 — title` inside
`## Tickets` are not ticket headings (the importer skips them). An anchor to
such an id refuses with
`row_source_anchor_not_found: no ticket heading for sl40 in path#sl40`
(exit 1). To import and show the ticket, rename the id to the grammar
(`SL40`, `SL41`). A `path:line@commit` locator shows the section of the nearest
heading of level three or less at or above that line, and refuses
(`not_found`) when that heading is not a ticket heading; a `### Ticket:`
heading whose tail is not that shape still counts there, with no id.
An item whose source is a row key `row:<ID>`
is read at the locator of its newest valid `also at <locator>` note whose
idempotency key starts with `row-item:also-at:` (the text is exactly
`also at ` and a locator, either `path:line@commit` or `path#ID`; a note of
any other shape, or one whose key is outside that namespace, is skipped).
A hand-written `also at` note (for example from `ledger note`) is skipped.
A `path#ID` locator names no line or commit: the section is the one whose
ticket heading, of either form, carries the id `ID` in `path` at the current
`HEAD` of the repository. With no such keyed note the command refuses with
`row_source_no_also_at`; a `path#ID` whose
heading is absent (a bare heading outside `## Tickets`, a fenced heading and a
table row do not count) refuses with `row_source_anchor_not_found`, and one
repeated (two ticket headings of either form with that id),
`row_source_anchor_ambiguous` (exit 1), reading nothing.

An item whose source names no file prints its current spec instead of
refusing, when it has one. The source names no file when it is empty, when it
is neither a locator nor a row key (a follow-up's `PARENT:slug` source), or
when it is a row key with no valid keyed `also at` note. The text is `spec: vN`, then
`title:`, `size:` and `merge_class:` (each only when the spec records it), a
`## Body` section and, when recorded, a `## Acceptance` section. With
`--json`, `data` carries the usual `item_id` and `ticket` (that text) and a
`spec` object with `version`, `title`, `size`, `merge_class`, `body_md` and
`acceptance`; a field the spec does not record is `null`. An item that read a
ticket from a file has no `spec` key. A source that does name a file reads and
refuses exactly as above: a broken locator, a missing file or a missing or
repeated anchor refuses even when the item has a spec, and the spec never
stands in for it. An item with no spec keeps the refusal it had
(`row_source_no_also_at`, `no source locator on ITEM` or `invalid source
locator: SOURCE`, all `not_found`, exit 1). A spec event that cannot be read is
refused `spec_unreadable` (exit 3), printing no text.

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
Then replay every event, in id order, through the child-of guard every write
and `restore` use, each event seeing only the ones before it. The first
`child-of` link that gives a child a second anchor without `moved_from`,
links an item to itself, names an anchor that is not yet an item, or makes a
cycle exits 4 with the message `event N CODE: ...`, where `CODE` (also the
JSON error code) is `second_anchor`, `self_link`, `anchor_missing` or
`cycle`. An event the guard cannot evaluate is reported the same way with
`unevaluable`, never skipped. A store that passes prints `ok events=N` and
exits 0, as before.

**`export [--dir DIR] [--commit | --push | --check [--max-events N] [--max-hours H]]`**
Write events after the watermark as canonical JSON files. `--commit` scans,
then makes one git commit in `DIR`, and never pushes; `--push` scans the
branch's whole history, then pushes its commit; `--check` reports the lag
and exits 3 when it is over the limits (100 events, 24 hours).
Before any event file is written, every pending document is scanned for the
credential shapes `doc add` refuses. A document holding one without the
`credential_override` marker refuses the whole export
`export_doc_credential` (exit 3), naming the event, its item and the shapes:
nothing is written, the watermark does not move, and `--commit` does not
run. A document attached with `--allow-credential-shapes` is exported. A
by-path document's event file carries its stored path, sha256 and size and
no bytes, so export never opens a document file; it scans the stored path
instead, and an unmarked hit there refuses `export_doc_credential` the same
way, naming the shapes as in the path and never the path.

The guard guarantees that every byte the ledger's own commit and push send
is reachable from a pinned commit whose full history passed the scan; it
does not guarantee which branch, index or destination ends up updated when
another same-user git process changes HEAD, refs or config while the
command runs.

`--commit` stages the export directory into a temporary index, never the
repository's own, and scans the exact tree it will commit before it makes
the commit. The temporary index does not carry skip-worktree bits from the
repository index, so a skip-worktree file is staged from the work tree as
it is on disk, not omitted. A file at an event name (sixteen digits and `.json`, at any
depth) must be the canonical bytes of a valid event record, and its
`doc.attached` document gets the same scan as at export; every other file,
every path name and the commit message are scanned for the credential
shapes, with no override. The commit's parent is the head the step read,
and only that branch moves, only if it still holds that head; the commit is
also refused if HEAD no longer names that branch when checked. A refusal
(codes below) commits nothing and leaves HEAD and the index as they were.
After a commit the index is set to the new commit's tree when HEAD still
names the branch, and the result's `index_refreshed` says whether that ran;
when it did not, the text line ends with a warning, and `git read-tree HEAD`
in the export directory sets the index. `--commit` signs its commit when
`commit.gpgSign` is true, as `git commit` would, and refuses
`export_commit_sign_failed` when signing fails.

`--push` needs HEAD on a branch. It refuses a shallow repository, and a push
remote with more than one push URL when it checks before the scan. It reads
the branch's commit once, scans every commit, path and file reachable from
it (the whole history: a file committed by hand, even one removed later,
refuses the push), then pushes exactly that commit to `refs/heads/<branch>`
at the branch's push remote: `branch.<name>.pushRemote`, else
`remote.pushDefault`, else `branch.<name>.remote`, using that remote's push
URL. There is no fallback to a remote named `origin`. It pushes no tag and
no other ref, sends no push option and no push certificate (it ignores
`push.gpgSign`), ignores `push.default` and `remote.<name>.push`, and never
opens the ledger. A refusal pushes nothing.
`export_push_failed` means git push itself failed after it started; the
remote may or may not hold the commit, so check it. With `--json` a push
reports `sha`, `pushed`, `branch` and `remote` (null when the push remote is
a URL or a path).

Both steps also scan the branch name and refuse `export_branch_credential`
without printing it. A history that already holds a refused file cannot be
pushed by the ledger: remove the file from the export repository's history
with git, and rotate the credential if that history has been pushed before.
The scan is the same tripwire as at `doc add`: a credential in another
shape, or encoded, passes.

Refusal codes of the export, commit and push steps, each exit 3:

- `export_head_detached`: HEAD is detached or names a ref outside `refs/heads/` (commit, push).
- `export_push_unborn`: the branch has no commit (push).
- `export_push_unconfigured`: no push remote is set by `branch.<name>.pushRemote`, `remote.pushDefault` or `branch.<name>.remote`, or the value starts with a hyphen (push).
- `export_push_multiple_urls`: the push remote has more than one push destination when counted before the scan (push).
- `export_repo_shallow`: the export repository is shallow (push).
- `export_history_incomplete`: a listed commit's object names a parent that the history listing does not hold (push).
- `export_doc_credential`: a valid `doc.attached` record holds an unmarked credential shape or undecodable bytes (export, commit, push).
- `export_file_credential`: a credential shape in a file that is not at an event name, in a path name or in a commit object (commit, push).
- `export_event_invalid`: a file at an event name is not the canonical bytes of a valid event record (commit, push).
- `export_object_unreadable`: a missing object, a wrong object type, a submodule (gitlink), or a branch that does not hold a commit (commit, push).
- `export_head_moved`: at the swap, the branch no longer holds the head the step read (or exists when it was unborn), or HEAD no longer names the branch (commit).
- `export_commit_ancestry`: the new commit's tree or parents are not exactly the scanned tree and the old head (commit).
- `export_branch_credential`: the branch name holds a credential shape; the name is not printed (commit, push).
- `export_commit_sign_failed`: `commit.gpgSign` is true and signing the commit failed; no ref moved (commit).
- `export_git_failed`: any other git command failed before the push command, or in the commit steps (commit, push).
- `export_push_failed`: the push command itself failed after it started; the outcome is unknown (push).

**`backup --to FILE`** Online backup with `PRAGMA integrity_check`. The
backup holds the database only: a by-path document's bytes live under its
document root, so copy that directory too, and put it beside the restored
database (see `restore`).

**`restore --dir DIR --system NAME [--accept-loop-events]`** Replay an
export into the new database named by `--db`, then verify it. The target
must not exist. A document holding a credential shape in its inline bytes or
its stored path without the `credential_override` marker refuses the whole
restore `restore_doc_credential` (a hit in a stored path is named by its
shape as in the path, never the path), and nothing is inserted. By-path documents restore
as references: restore checks their stored path, storage and size against
the schema and never opens a file. Their paths resolve against the restored
database's own document root (`<name>.documents` beside the database `--db`
names), never the source's, so `doc verify` reports them `missing` until you
copy the old document root's tree there; then run `doc verify` to check
every file against its stored sha256.

## Guidance

**`guidance set|list|withdraw [--pool POOL] [--direction burn|protect] [--reason R] [--starts DATE] [--expires DATE]`**
Guidance is a dated note that a named resource pool (a usage quota, a
budget) should be spent down (`burn`) or held back (`protect`), and why. One
item stands for one pool and direction; setting it again appends a new
version and the latest wins; `withdraw` closes it early. Guidance is advice
for whatever reads it: the ledger records it and enforces nothing.

## Resume

**`resume [--item ID] [--brief] [--hook-env]`**
For each open item that has an `item.stopped` event, print the newest stop
(its point, the skills to invoke next, the files to read) and the commands
that continue it, using the command prefix the person ran. The printed command
includes `--db PATH` only when this invocation received `--db`; with
`LEDGER_DB`, it relies on that environment setting. With `--item`, show only
that item (exit 1 when it does not exist). `--brief` first prints the short
form of the agent contract ([agent-contract.md](agent-contract.md)), so a
session-start hook can print both. It never writes. With no items it prints
`no open items`; when open items exist but none has a stop, it prints
`N open item(s) with no stop recorded; ledger query lists them`. Both exit 0.
`--json` carries `items` (one entry per stopped open item, as the text lists
them) and `open_without_stop`, always present: how many open items have no
stop recorded and so are not in `items` (0 when there are none, and 0 with
`--item`). `--brief` adds `contract`. Two open items, neither stopped:

```json
{"data": {"items": [], "open_without_stop": 2}, "error": null, "ok": true}
```

`--hook-env` is for a SessionStart hook (the plugin's runs it). It reads the
hook's JSON input on stdin and prints the lines the hook appends to Claude
Code's `CLAUDE_ENV_FILE`, each `export NAME=<value>` with the value
shell-quoted, in this order:

| Line | Printed when |
|------|--------------|
| `LEDGER_SESSION=<session_id>` | the input's `session_id` is usable, even when `LEDGER_SESSION` is already set |
| `LEDGER_AGENT=claude-code` | `LEDGER_AGENT` is absent from the environment |
| `LEDGER_HARNESS=claude-code` | `LEDGER_HARNESS` is absent from the environment |

It prints no `LEDGER_MODEL` line, whatever the input's `model`. Only
SessionStart, Setup, CwdChanged and FileChanged hooks can write
`CLAUDE_ENV_FILE`, and a model switch during the session is none of them, so
a model stamped at session start would go stale and every later write would
record it. Writes in a session the plugin started record no model unless
`LEDGER_MODEL` is already set in Claude Code's environment.

A variable set to an empty value counts as set. A usable value is a non-empty
string with no control character; any other value leaves its line out and is
not an error. The command exits 0 even when it prints no line; `--json`
carries `{"env": {NAME: value, ...}}`. Stdin that is not UTF-8, not JSON or not
an object is refused with `hook_env_bad_input`, and an input whose
`hook_event_name` is not `SessionStart` with `hook_env_not_session_start`
(exit 3, nothing on stdout). With `--brief` or `--item` it is a usage error
(exit 2). It never resolves, opens or creates the database, though `--db` or
`LEDGER_DB` is still required, as for every verb.

## Contract

**`contract`** Print the full agent contract
([agent-contract.md](agent-contract.md)) from the installed package, so an
agent that has only the package can read the whole page; `resume --brief`
prints its short form and points here. It never opens the database, though
`--db` or `LEDGER_DB` is still required, as for every verb. With `--json` the
page is the `contract` field.
