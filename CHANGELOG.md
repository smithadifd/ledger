# Changelog

## 0.1.5 — 2026-11-05

Two fixes for everyday agent use. Nothing is renamed, and every command that worked in 0.1.4 works the same way.

- **A write no longer makes the search index read "stale".** After a rebuild, any ordinary write (a note, or a session's `item.stopped`) used to turn `doctor` to `search: stale`, though search kept working. The index now has a `behind` state: it is a clean prefix of the event log and newer events exist. `doctor` prints `search: current to event N, M new event(s) will be indexed on the next search`, and its JSON status is `behind`. `stale` now means a real mismatch: a changed format, ranker, database or roots; an added, edited or deleted source file; or an event log that no longer matches the record. Search still rebuilds before answering whenever the index isn't `current`, so it never answers from an index that is missing events (LC27).
- **`note`, `link` and `elect` take `--idempotency-key`,** with the same retry rule as `append` and `pick`. The same key with the same content returns the stored event and writes nothing. The same key with different content is refused as `conflict` (exit 3). Before this, these three verbs always minted a fresh key, so an agent that retried one after a timeout wrote the event twice. `link --rel duplicate-of` covers both of its events with one key, and refuses as `partial_write` if only one of them is present. See "Retrying with a key" in `docs/cli.md` (LC28).

Upgrade with `@v0.1.5`.

## 0.1.4 — 2026-11-05

The `ledger link --help` example now names `~/notes/brief.md` instead of a path under `/home`, so no shipped file in the package names a user's home directory. A consumer that installs the package into a profile refusing such paths can now vendor it. A new top-level test keeps the package free of home-directory paths. Nothing else changes. Upgrade with `@v0.1.4`.

## 0.1.3 — 2026-11-05

The package no longer names any path in this repository, so a copy of `ledger/` vendored on its own stands alone. The tests that read the repository's README and agent-contract page moved out of `ledger/tests/` into the top-level `tests/` folder, which is not vendored; this also fixes a bug for consumers who vendor the package inside their own project, where the old check mistook the consumer's `pyproject.toml` for this repository's and ran those tests against the wrong files (LC19). A new top-level test fails if any tracked file under `ledger/` names a repository path again. The CLI is unchanged except the wording of the short form `ledger resume --brief` prints (it points at the contract page in words, and its example `refs` value is now `plan.md`). Upgrade with `@v0.1.3`.

## 0.1.2 — 2026-11-05

Test-only release; the CLI is unchanged from 0.1.1. Two tests that read the repository's README and agent-contract page now skip by name in a copy of `ledger/` vendored without them, and `LEDGER_REPO_ROOT` points them at a repository root. Upgrade the same way as 0.1.1, with `@v0.1.2`.

## 0.1.1 — 2026-11-05

This release is for the agent using the ledger, not only the person who installed it.
Nothing is renamed and every command that worked in 0.1.0 still works the same way.

### New: `ledger resume`

- `ledger resume` lists every open item that has a stop stamp: where it stopped, the
  phase, the skills to run next, the files to read, and the exact commands to continue.
- `ledger resume --item <id>` shows one item.
- `ledger resume --brief` prints a short form of the agent contract first, then the
  list. This is what the sdlc plugin's session-start hook prints (plugin 0.11.0), so
  every session starts knowing the ledger exists, what to write without asking, and
  where the last session stopped. It only reads; with no database yet it prints
  `no open items`.
- The full agent-facing page is `docs/agent-contract.md`: when to write, the reading
  order (`resume`, `card`, then `export-item` when the card shows a cut), what each
  `LEDGER_*` stamp is for, and the close rule (write `item.stopped`, then print the
  resume command).
- The card's default next step now reads `Resume with: ledger resume --item <id>`.

### Verbs work on the first try

- `ledger note <item> "text"` works (as well as `--text`).
- `ledger link <item> <target>` works, with `--rel` defaulting to `url`; a local file
  path is a fine target.
- `ledger search several words` works without quotes.
- `note`, `link` and `elect` `--help` end with worked examples. `elect --help` says
  `--rejected` is required and repeatable.
- `ledger query --since/--until` filters items by date, with the same date forms as
  `search`.

### Knowing you have the whole record

- `ledger export-item` ends with one line: events returned, events on the item, and
  events decayed (`--json` carries the same numbers as `data.completeness`).
- Every `(+K cut)` marker on a card names the command that recovers it, and the footer
  says the events are the authority.
- `search` refuses by name, instead of crashing, when it cannot rebuild a stale index.

### Upgrading

Install the tag into the environment you already use, for example:

    ~/.local/share/ledger/venv/bin/pip install "git+https://github.com/smithadifd/ledger.git@v0.1.1"

Then check it with `ledger resume --brief`: it should print the short contract. No
database migration is needed.

For session ids on every event, let the sdlc plugin 0.11.0 hook set `LEDGER_SESSION`,
and set `LEDGER_AGENT` yourself if you want a name other than `claude-code`.
