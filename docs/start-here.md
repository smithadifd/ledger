# Start here

This page takes you from nothing to a working ledger, in order. Do each step
and run its check before the next one. Every command is one you can run
as written; `vX.Y.Z` is the one thing you fill in.

## 1. Python 3.11 or newer

```sh
python3 --version
```

It must print 3.11 or later. The ledger needs nothing else from Python: it
has no runtime dependencies.

## 2. pipx

[pipx](https://pipx.pypa.io/) installs a command-line program into an
environment of its own and puts it on your `PATH`. Check for it:

```sh
pipx --version
```

If that prints "command not found", install it by the steps at
https://pipx.pypa.io/stable/how-to/install-pipx.html, then run `pipx ensurepath` and open
a new terminal. Do not use a bare `pip install` for this: current macOS and
Homebrew Python refuse it (PEP 668, `externally-managed-environment`).

## 3. Install the ledger at a tag

Pick a release tag from https://github.com/smithadifd/ledger/tags and put it
where `vX.Y.Z` is:

```sh
pipx install git+https://github.com/smithadifd/ledger@vX.Y.Z
ledger --version
```

The second command prints `ledger` and the version you installed. If your shell
says `ledger: command not found`, run `pipx ensurepath` and open a new
terminal. To move to a newer tag later, see
[Upgrade](../README.md#upgrade); it takes one command and nothing else.
From 0.1.8 or older, read [Upgrading across many releases](upgrading.md) first.

## 4. Decide where the database lives

The ledger has no default database path. Every call needs `--db PATH` or the
`LEDGER_DB` environment variable, and without either it stops with
`pass --db or set LEDGER_DB`. The supported location is
`~/.local/share/ledger/<system>.db`, one file per system (or whatever
`LEDGER_DB` names):

```sh
export LEDGER_DB=~/.local/share/ledger/work.db
```

Use a short name of your own for the body of work this database holds, in
place of `work` (`<system>` above). Keep the file out of a synced folder (iCloud,
Dropbox, OneDrive, Syncthing) and out of a git working tree; the ledger
refuses a synced root.

## 5. Set the environment for writes

```sh
export LEDGER_HARNESS=cli
export LEDGER_AGENT=me
```

- `LEDGER_HARNESS` is required for every write. It says what is writing: a
  tool, a script or an agent runtime, so each event records its source. Use
  `cli` when you type the commands yourself; an agent runtime uses its own
  name, for example `claude-code`.
- `LEDGER_AGENT` is optional. It names the person or process writing, and
  the actor stamped on each event is `<operator>/<agent>` when `LEDGER_OPERATOR`
  is set as well.
- `LEDGER_DB` is the path from step 4.

The plugin or harness that starts an agent session normally exports these for
the agent. Reads such as `doctor` and `verify` do not need `LEDGER_HARNESS`.

## 6. Make the first write

There is no `init` verb. The first write creates the database file, and the
folders above it, and applies every migration:

```sh
ledger new --type ticket --title "My first item" --id first-1
```

It prints one line that starts with `created` and names `first-1`. Run it
only once: a second run with the same id is refused.

## 7. Check with `doctor`

```sh
ledger doctor
ledger verify
```

A healthy result exits 0 and prints three lines:

```text
path: <the database path from step 4>
schema_version: N
search: missing (ledger index rebuilds it)
```

`N` is the newest migration applied. `search: missing` is normal until your
first `ledger search`; it is the optional search index, not a fault. `verify`
prints `ok events=` and a count. `doctor` also lists any claim whose lease has
expired, one line each; a new database has none.

If `doctor` says `database does not exist`, it is looking at a path where
nothing has been written: check `echo $LEDGER_DB` and run step 6. If it says
`pass --db or set LEDGER_DB`, step 4 was not done in this shell.

## 8. Optional: connect Claude Code

To have Claude Code print the ledger's brief when a session starts and record
your prompts and question-picker answers, install the ledger's plugin from
Claude Code:

```sh
claude plugin marketplace add smithadifd/ledger
claude plugin install ledger-hooks@ledger-plugins
```

Its hooks do nothing until `ledger` is on Claude Code's `PATH` and `LEDGER_DB`
is set in the environment Claude Code starts with. The README's
[Claude Code plugin](../README.md#claude-code-plugin) section says what each
hook does.

## Next

- The [README](../README.md) has a longer quick start, the model, and the verb table.
- [docs/cli.md](cli.md) is the reference for every verb, flag and exit code.
- Optional: skills for Claude Code that keep their state in the ledger live in
  the skills repository, https://github.com/smithadifd/skills.
  Follow the steps there; this page does not repeat them.
