# The agent contract

This is the one page an agent needs. If you are an agent working in a project
that has a ledger, read this once; `ledger resume --brief` prints the short
form of it at the start of every session, and `ledger contract` prints this
whole page from the installed package.

The ledger is where this work's state lives: what is open, what was decided,
where the last session stopped. It is append-only and every write is stamped
with who made it. Your session's memory ends; the ledger does not.

## Write triggers

Write to the ledger at each of these moments, and write without asking. A
person should never have to remind you; asking first is the failure this page
exists to prevent.

- **item opened** — a new piece of work starts: `ledger new --type TYPE --title TITLE`.
- **item picked up** — you start work on an item that already exists:
  `ledger claim ITEM`.
- **item closed** — the work is done or dropped: `ledger state ITEM --status closed`.
- **decision made** — a choice was settled in the work and the reason should
  outlive the session: `ledger note ITEM --text "decided: ... because ..."`.
- **decision elected** — the person chose between options: ask through the
  question picker, then `ledger pick ITEM --ask K` (see below).
- **brief ratified** — the person approved a brief or plan. A ratification is
  an election, not a note: ask for it through the picker and `ledger pick`
  the answer. Link the brief itself by path with
  `ledger link ITEM --rel url --target PATH`.
- **follow-up filed** — work in a session turns up something to do later (a
  hardening finding, a flake, a deferred piece of your own work): make a
  source `SRC` from the parent's id, a colon and a short slug of the
  follow-up (`it-1a2b3c4d5e6f:flaky-retry`), create the follow-up with
  `ledger new --type TYPE --title TITLE --source SRC`, then link it to the
  item it came from with
  `ledger link NEW --rel child-of --target PARENT`.
  If you are interrupted between the two, run both again unchanged, even
  from a later session: the same source reaches the same child, and a link
  it already has to that parent writes nothing, so one link remains.
- **thread parked** — work pauses with more to do: write `item.stopped`.
- **thread stopped** — a session ends mid-item: write `item.stopped`.

Use the verbs `note`, `link`, `ask-display`, `pick` and `elect`. Never write
a hand-made append script. Two events have no verb of their own and go
through one plain `ledger append` command each: `decision.asked` (the
question you put to the person) and `item.stopped` (see the close rule).
Never write the person's words yourself: the harness captures them.

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
the person is a decision, and a decision is an election. A gate counts it
only when the person answered in the question picker and the harness
captured that answer for this question.

1. Record the question as an ask, with two to four options and the scope
   the answer covers:
   `ledger append --item ITEM --kind decision.asked --payload '{"question": "...", "options": [{"label": "apply", "description": "..."}, {"label": "hold", "description": "..."}], "scope": "..."}'`.
   The output names the event id, K.
2. `ledger ask-display K` prints the picker input. Send it as your
   AskUserQuestion call exactly as printed; never write the picker yourself.
3. Once the person has answered, the harness has recorded the answer. Record
   the election from it: `ledger pick ITEM --ask K`.

`pick` takes nothing else: the chosen option, the rejected ones, the
question, the scope and the quote all come from the ask and the captured
answer. It refuses when no answer was captured (`capture_missing`), when two
answers disagree (`capture_conflict`), when the person typed an answer or
added a note (`picker_typed`, `picker_annotated`), or when the picker shown
was not the one `ask-display` printed (`display_differs`). Ask again with a
fresh ask; never work around a refusal.

The picker prints the ask's question id, scope, question, and each option's
label and description, and the answer binds to exactly that text. So none of
it may hold a control character: C0 (U+0000-U+001F, which includes ESC and so
every ANSI escape), DEL (U+007F) or C1 (U+0080-U+009F). Such an ask is
refused when you write it (`ask_control_character:<field>`, naming the
field, for example `ask_control_character:question_id`); the one exception
is a newline (U+000A) in the question text, which a long question may use.
The question id, scope, labels and descriptions take no newline, and a
carriage return or tab is refused everywhere. An ask stored before this rule
that holds one reads `ask_not_pickable` (the reason names the field and
"control character"), and no pick, election or grant binds through it. The
same text in an election is refused the same way. The rule refuses; it never
strips or escapes, so write the ask again without the character.

A typed answer in the chat can still be recorded as evidence with
`ledger elect`, quoting the harness's capture of that prompt, but no gate
opens on it. Never run `ledger capture`: it belongs to the harness's hooks.
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

The database path comes from `LEDGER_DB` (or `--db`). There is no default
path; the supported location is `~/.local/share/ledger/<system>.db`, or
whatever `LEDGER_DB` names.

## The close rule

End every session by writing an `item.stopped` event on each item you worked,
then print the exact resume command for the next session. Print the command,
never a prose summary of it.

```sh
ledger append --item ITEM --kind item.stopped \
  --payload '{"point": "what is done and what is next", "phase": "build", "skills": ["review"], "refs": ["plan.md"]}'
ledger resume --item ITEM
```

`phase` is one of `orient`, `plan`, `build`, `review`, `close`. `skills` and
`refs` are optional: the skills the next session should invoke and the files,
pull requests or event ids it should read. The newest stop on an item is the
one `resume` shows.
