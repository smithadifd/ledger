# Start here

This page takes you from nothing to a working ledger, in order. Do each step
and run its check before the next one. Every command is one you can run
as written; `vX.Y.Z` is the one thing you fill in, and step 3 prints what goes
there.

Setup has four parts, in this order, because each reads only what an earlier
part made:

1. the `ledger` command (steps 1 to 3);
2. the environment, which names the database (steps 4 and 5);
3. the `ledger-hooks` plugin for Claude Code (step 9);
4. the skills plugin and its setup step (step 10).

The first write, which creates the database, comes after the environment and
before the check (steps 6 to 8).

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

If that prints "command not found", install it. On a Mac with
[Homebrew](https://brew.sh/), two commands do it:

```sh
brew install pipx
pipx ensurepath
```

`brew install pipx` puts the `pipx` command in Homebrew's folders, with the
current Python it depends on. `pipx ensurepath` adds one line to your shell's
profile file, so that `~/.local/bin`, where pipx links the commands it
installs, is on your `PATH`. Open a new terminal, then run `pipx --version`
again. From then on pipx keeps each program it installs in a folder of its own
(`pipx environment` lists them). Without Homebrew, follow
https://pipx.pypa.io/stable/how-to/install-pipx.html. Do not use a bare
`pip install` for this: current macOS and Homebrew Python refuse it
(PEP 668, `externally-managed-environment`).

## 3. Install the ledger at a tag

This step needs `git`, as the install does (on a Mac without it, macOS offers
to install Apple's command line tools the first time you run `git`). Print the
newest release tag:

```sh
git ls-remote --tags --refs --sort=-v:refname https://github.com/smithadifd/ledger 'v*' | sed -n '1s|.*refs/tags/||p'
```

Put what it prints where `vX.Y.Z` is (https://github.com/smithadifd/ledger/tags
lists every tag):

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
refuses a synced root. The shell expands `~` to an absolute path, and the path
must be absolute: a relative one resolves against whatever folder a session
starts in.

## 5. Set the environment

In your shell, set the two values a write needs besides the database:

```sh
export LEDGER_OPERATOR=me
export LEDGER_HARNESS=cli
```

Put step 4's line and these two in your shell profile (for zsh, `~/.zshrc`)
so every new terminal has them.

- `LEDGER_HARNESS` is required for every write. It says what is writing: a
  tool, a script or an agent runtime, so each event records its source. Use
  `cli` when you type the commands yourself; an agent runtime uses its own
  name, for example `claude-code`.
- `LEDGER_OPERATOR` names the person the writes are made for. `LEDGER_AGENT`
  is optional; it names the process writing, and the actor stamped on each
  event is `<operator>/<agent>` when both are set.
- `LEDGER_DB` is the path from step 4.

Claude Code starts its sessions with an environment of its own, so give it the
database and the operator too: add them to the `env` block of its settings
(`~/.claude/settings.json` for your user), adding keys and keeping any that
are already there. Write the database path in full; `echo
~/.local/share/ledger/work.db` prints it:

```json
{
  "env": {
    "LEDGER_DB": "/absolute/path/to/work.db",
    "LEDGER_OPERATOR": "me"
  }
}
```

Leave `LEDGER_HARNESS` out of the settings: the plugin's session-start hook
(step 9) sets it to `claude-code` when it is unset. A session started from a
shell that exports `LEDGER_HARNESS=cli` inherits that value instead, so run
`unset LEDGER_HARNESS` before you start Claude Code, or put
`LEDGER_HARNESS=cli` in front of the commands you type. Reads such as `doctor`
and `verify` do not need `LEDGER_HARNESS`.

## 6. Make the first write

There is no `init` verb. The first write creates the database file, and the
folders above it, and applies every migration, so there is no folder to make
first:

```sh
ledger new --type ticket --title "My first item" --id first-1
```

It prints one line that starts with `created` and names `first-1`. Run it
only once: a second run with the same id is refused.

## 7. Claim the database for its system

```sh
ledger claim-system work
```

Use the same short name you chose in step 4. Each system, for example `work`
and `home`, has its own database file, and this step writes the system's name
inside that file. The claim is part of the file, so any path or link that
reaches the file reaches the claim: the boundary between two systems is the
file itself, not a naming habit, and an export or a restore carries it. The
command prints `claimed for work: <path>`. Running it again with the same name
prints `already claimed for work` and changes nothing; running it with another
name is refused, so two systems cannot end up in one database. A system name is
a lowercase letter followed by lowercase letters, digits, `_` or `-`, up to 64
characters.

## 8. Check with `doctor`

`doctor` reads a database that exists, so it fails with `database does not
exist` until a write (step 6) has made the file; that is why the check comes
after the first write and not before it.

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

## 9. Connect Claude Code: the hooks plugin

To have Claude Code print the ledger's brief when a session starts and record
your prompts and question-picker answers, install the ledger's plugin from
Claude Code:

```sh
claude plugin marketplace add smithadifd/ledger
claude plugin install ledger-hooks@ledger-plugins
```

Its hooks do nothing until `ledger` is on Claude Code's `PATH` and `LEDGER_DB`
is set in the environment Claude Code starts with, which steps 3 and 5 did.
The brief goes into the model's context; the terminal does not show it. Run
`ledger resume --brief` to read the same text. The question-picker flow, where
the ledger records a person's answer to a question, needs an interactive
Claude Code session: `claude -p` shows no picker. The README's
[Claude Code plugin](../README.md#claude-code-plugin) section says what each
hook does, and [Asking a question](../README.md#the-model) shows the payload
for a question.

## 10. Optional: the skills plugin

Skills for Claude Code that keep their state in the ledger live in the skills
repository, https://github.com/smithadifd/skills. Install its plugin and run
its setup step, which pins its `ledger_cli` setting to the command from step 3.
Follow the steps there; this page does not repeat them.

## 11. Optional: a scheduled export

Once the ledger works, [A scheduled export without the toolkit](scheduled-export.md)
sets up a private git repository and jobs that export, push and check it on a
schedule, and shows how to prove the pushed copy restores.

## Next

- The [README](../README.md) has a longer quick start, the model, and the verb table.
- [docs/cli.md](cli.md) is the reference for every verb, flag and exit code.
- To remove it all again, see [Uninstall](../README.md#uninstall).
