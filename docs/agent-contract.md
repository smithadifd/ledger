# The agent contract

This is the one page an agent needs. If you are an agent working in a project
that has a ledger, read this once; `ledger resume --brief` prints the short
form of it at the start of every session.

The ledger is where this work's state lives: what is open, what was decided,
where the last session stopped. It is append-only and every write is stamped
with who made it. Your session's memory ends; the ledger does not.

## Write triggers

Write to the ledger at each of these moments, and write without asking. A
person should never have to remind you; asking first is the failure this page
exists to prevent.

- **item opened** — a new piece of work starts: `ledger new --type TYPE --title TITLE`.
- **item closed** — the work is done or dropped: `ledger state ITEM --status closed`.
- **decision made** — a choice was settled in the work and the reason should
  outlive the session: `ledger note ITEM --text "decided: ... because ..."`.
- **decision elected** — the person chose between options: `ledger elect`,
  quoting the words they typed (see below).
- **brief ratified** — the person approved a brief or plan. A ratification is
  an election, not a note: capture their words, then `ledger elect` quoting
  them. Link the brief itself by path with
  `ledger link ITEM --rel url --target PATH`.
- **thread parked** — work pauses with more to do: write `item.stopped`.
- **thread stopped** — a session ends mid-item: write `item.stopped`.

Use the verbs `note`, `link` and `elect`. Never write a hand-made append
script. Two events have no verb of their own and go through one plain
`ledger append` command each: `utterance.captured` (the person's words, the
evidence an election quotes) and `item.stopped` (see the close rule).

## Reading order

When you start, or come back to an item:

1. `ledger resume` lists every open item with a stop stamp: where it stopped,
   the skills to invoke next, the files to read, and the exact command to
   continue. `ledger resume --item ID` shows one item.
2. `ledger card ITEM` is the bounded card: fields, decisions, open questions,
   the next step, notes.
3. `ledger export-item ITEM` prints the item's full event history. Read it
   when the card shows a cut (a `Truncated` section), because the card is a
   projection and the events are the record.

## Electing a ratification

A ratification, a choice between options, or any "yes, do it that way" from
the person is a decision, and a decision is an election.

1. Capture the person's words as an utterance:
   `ledger append --item ITEM --kind utterance.captured --channel terminal --provenance-class submitted-prompt --payload '{"text": "...", "origin": "person"}'`.
   The output names the event id.
2. Elect, quoting that text byte for byte:
   `ledger elect ITEM --question Q --chosen LABEL --chosen-description D --rejected LABEL --scope S --quote-ref EVENT --quote "words they typed"`.

Never record only a note such as "approved by the person". A note carries no
quoted evidence, and `ledger lint-quotes` can only check an election.

## Actor stamps

Every event is stamped from the environment. Set these before you write; the
plugin or harness that starts your session normally exports them.

| Variable | What it is for |
|----------|----------------|
| `LEDGER_OPERATOR` | the person on whose behalf you write |
| `LEDGER_AGENT` | which seat or process is writing (the actor is `<operator>/<agent>`) |
| `LEDGER_HARNESS` | the runtime you run in; every write refuses without it |
| `LEDGER_MODEL` | the model id, so a later reader knows what wrote it |
| `LEDGER_SESSION` | the session id, so every event of one session can be found together |

The database path comes from `LEDGER_DB` (or `--db`).

## The close rule

End every session by writing an `item.stopped` event on each item you worked,
then print the exact resume command for the next session. Print the command,
never a prose summary of it.

```sh
ledger append --item ITEM --kind item.stopped \
  --payload '{"point": "what is done and what is next", "phase": "build", "skills": ["review"], "refs": ["docs/plan.md"]}'
ledger resume --item ITEM
```

`phase` is one of `orient`, `plan`, `build`, `review`, `close`. `skills` and
`refs` are optional: the skills the next session should invoke and the files,
pull requests or event ids it should read. The newest stop on an item is the
one `resume` shows.
