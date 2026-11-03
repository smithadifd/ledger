# Working with agent skills

The ledger is the store behind two skills in
[smithadifd/skills](https://github.com/smithadifd/skills): `topic`, which
keeps the notes of one ongoing thread, and `ticket`, which takes one ticket
through plan, build, review and an approved outward step. Neither skill ships
a ledger. Each states the verbs, flags, envelope fields and error codes it
calls as a contract and is pointed at a CLI that meets it: `topic` through
the `ledger_cli` key of its `config.json`, `ticket` through the
`TICKET_LEDGER_CLI` environment variable. This page walks both contracts
against a fresh database, so you can see what each skill reads and writes
and check the same thing against your own setup.

Every command below runs with `--json`, which is how the skills call the
CLI: exactly one JSON object on stdout, `{"ok": true, "data": ...}` or
`{"ok": false, "error": {"code": ..., "message": ...}}`. The transcript
shows the fields the skills read and elides the rest.

```sh
export LEDGER_DB=/tmp/ledger-skills-demo/ledger.db
export LEDGER_HARNESS=demo
mkdir -p /tmp/ledger-skills-demo
```

## The `topic` contract

A thread is an item of type `topic` with the id `topic-<slug>`. The skill
writes no note file: the item's notes are the thread.

| Call | What the skill reads |
|------|----------------------|
| `query --type topic --item <id>` | `data.items[]` with `item_id` and an integer `revision`; error code `empty` or `missing_db` when there is no such item |
| `new --type topic --title <slug> --id <id>` | `data.event.id`; error code `conflict` when the id exists |
| `note <id> --text <text> --expect-revision <n>` | `data.event.id`; error code `stale_revision` when the item moved |
| `export-item <id>` | `data.events[]`, each with `kind` and `payload` |
| `card <id>` | a readable summary |
| `link <id> --rel url --target <path>` | attaches a document |
| `state <id> --status closed` | closes the thread |

Opening a thread that does not exist yet:

```sh
$ ledger --json query --type topic --item topic-demo
{"ok": false, "error": {"code": "missing_db", "message": "database does not exist: ..."}}   # exit 1
$ ledger --json new --type topic --title demo --id topic-demo
{"ok": true, "data": {"item_id": "topic-demo", "event": {"id": 1, "kind": "item.created", ...}}}
$ ledger --json query --type topic --item topic-demo
{"ok": true, "data": {"items": [{"item_id": "topic-demo", "type": "topic", "status": "open", "revision": 1, ...}]}}
```

The skill keeps the thread's running state (its staged drafts and its
promote-or-park list) in a note with a fixed grammar, and appends a newer
note of the same shape to change it. Each note carries the revision the
skill last saw, so a note written from stale knowledge is refused rather
than interleaved:

```sh
$ ledger --json note topic-demo --text "Ask: show the interplay" --expect-revision 1
{"ok": true, "data": {"item_id": "topic-demo", "event": {"id": 2, "kind": "item.note", ...}}}
$ ledger --json note topic-demo --text "written from a stale card" --expect-revision 1
{"ok": false, "error": {"code": "stale_revision", "message": "stale revision for item topic-demo: expected 1, current is 2 (event 2 written by actor=... session=-)"}}   # exit 5
```

A long write-up is a document the session writes wherever the project keeps
its documents, linked from the item. Closing the thread is one state event:

```sh
$ ledger --json link topic-demo --rel url --target docs/notes/demo.md
{"ok": true, "data": {"event": {"id": 3, "kind": "item.linked", "payload": {"rel": "url", "target": "docs/notes/demo.md"}, ...}}}
$ ledger card topic-demo
# topic-demo
revision: 3 <hash of event 3>
source_event_ids: 1, 2, 3
type: topic
title: demo
status: open
...
## Links
- url: docs/notes/demo.md (event=3)

## Notes
- event=2: Ask: show the interplay

This card is a projection, not authority
$ ledger --json state topic-demo --status closed
{"ok": true, "data": {"event": {"id": 4, "kind": "item.state", "payload": {"status": "closed"}, ...}}}
```

On a later resume, the skill reads `card` (or `export-item` when the card
marks notes as cut), reconciles the thread against the documents it links,
and records the pass as one more note.

## The `ticket` contract

With `TICKET_LEDGER_CLI` set, the ticket skill keeps its resume card as the
events of one item. Its token is the item's newest event id, and each step
of a pass is one `append-many` batch under `--expect-revision`: all of the
step lands or none of it does, and a lost race comes back as
`stale_revision`, which the pass reports and stops on. The skill checks the
contract before it reads or writes anything: `append-many --help` must exit
0 and name `--expect-revision`.

| Call | What the skill reads |
|------|----------------------|
| `append-many <item> [--expect-revision <n>]` | a JSON array of events on stdin, each with `kind`, `payload`, `item_id` and an `idempotency_key`; `data.events[-1].id` is the new revision; `stale_revision` and `conflict` are lost races |
| `query --item <item>` | `data.items[]` with `item_id`, `type`, `revision`, `owner` and `claim`; `not_found` or `empty` means no such item |
| `export-item <item>` | `data.events[]` in id order, each with `id`, `kind`, `payload` and `idempotency_key` |

```sh
$ ledger append-many --help | grep -c -- '--expect-revision'
2
$ ledger --json new --type ticket --title "Write the quick start" --id tk-1
{"ok": true, "data": {"item_id": "tk-1", "event": {"id": 5, ...}}}
$ ledger --json query --item tk-1
{"ok": true, "data": {"items": [{"item_id": "tk-1", "type": "ticket", "revision": 5, "owner": null, "claim": null, "status": "open", ...}]}}
```

One step of the pass is a batch. Here it records the plan and claims the
item, under the revision the pass read:

```sh
$ cat step.json
[{"kind": "item.note",  "item_id": "tk-1", "payload": {"text": "plan: three steps"}, "idempotency_key": "tk-1:plan:1"},
 {"kind": "item.state", "item_id": "tk-1", "payload": {"status": "claimed"},         "idempotency_key": "tk-1:claim:1"}]
$ ledger --json append-many tk-1 --expect-revision 5 < step.json
{"ok": true, "data": {"item_id": "tk-1", "events": [{"id": 6, "kind": "item.note", ...}, {"id": 7, "kind": "item.state", ...}]}}
$ ledger --json append-many tk-1 --expect-revision 5 < step.json
{"ok": false, "error": {"code": "stale_revision", "message": "stale revision for item tk-1: expected 5, current is 7 (...)"}}   # exit 5
```

A pass that crashed after the batch landed replays the same step on resume.
The idempotency keys make that safe: the replay returns the stored events
and writes nothing, and the chain is unchanged.

```sh
$ ledger --json append-many tk-1 < step.json
{"ok": true, "data": {"item_id": "tk-1", "events": [{"id": 6, "idempotency_key": "tk-1:plan:1", ...}, {"id": 7, "idempotency_key": "tk-1:claim:1", ...}]}}
$ ledger --json export-item tk-1
{"ok": true, "data": {"item_id": "tk-1", "events": [{"id": 5, "kind": "item.created", ...}, {"id": 6, "kind": "item.note", "idempotency_key": "tk-1:plan:1", ...}, {"id": 7, "kind": "item.state", "idempotency_key": "tk-1:claim:1", ...}]}}
$ ledger verify
ok events=7
```

## Writing your own integration

The same shape serves any tool:

- Read the item with `query --item` (or `export-item` for the full history)
  and remember its `revision`.
- Write each step as one `append-many` under `--expect-revision`, with an
  idempotency key per event derived from the step, so a replay is a no-op.
- Treat `stale_revision` and `conflict` as "someone else moved the item":
  re-read and decide again. Treat `validation`, `integrity` and
  `missing_harness` as permanent refusals.
- Set `LEDGER_HARNESS` to the name of your tool, so every event says it
  wrote the line.
