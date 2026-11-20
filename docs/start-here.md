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

If it prints an older version, such as 3.9, install a newer Python first.
On a Mac without [Homebrew](https://brew.sh/), use the macOS installer from
https://www.python.org/downloads/macos/. Open a new terminal afterwards and
check the version the installer put in place, naming its minor version (here
3.12; use the one you installed):

```sh
python3.12 --version
```

It must print 3.11 or later. Where this page then says `python3.12`, use the
command that printed it.

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

If you cannot get pipx, skip it: step 3 shows a second way to install the
ledger, into a virtual environment made with the Python from step 1.

## 3. Install the ledger at a tag

This step needs `git`, as the install does (on a Mac without it, macOS offers
to install Apple's command line tools the first time you run `git`). Print the
newest release tag:

```sh
git ls-remote --tags --refs --sort=-v:refname https://github.com/smithadifd/ledger 'v*' | sed -n '1s|.*refs/tags/||p'
```

If the command failed because there is no network, reconnect and run it again.
If it ran and printed nothing, use the tag named on
the [latest release page](https://github.com/smithadifd/ledger/releases/latest)
instead.

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

With no pipx, install into a virtual environment made with the Python from
step 1 (the tag is the one printed above; `python3.12` is the command that
printed 3.11 or later):

```sh
mkdir -p ~/ledger-venv
cd ~/ledger-venv
python3.12 -m venv .venv
.venv/bin/python -m pip install git+https://github.com/smithadifd/ledger@vX.Y.Z
.venv/bin/ledger --version
```

The command is then `~/ledger-venv/.venv/bin/ledger`. Either type that full
path wherever this page says `ledger`, or put the folder on your `PATH` with
this line in your shell profile and open a new terminal:

```sh
export PATH="$HOME/ledger-venv/.venv/bin:$PATH"
```

The hooks in step 9, from the v0.4.0 pin, run the plugin's own bundled
`bin/ledger`, so they do not need this folder on the `PATH` Claude Code starts
with. They run the first `ledger` on that `PATH` only when the bundled file is
missing or not executable, and in that case this folder must be on it.

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
database, the operator and the harness too: add them to the `env` block of its
settings
(`~/.claude/settings.json` for your user), adding keys and keeping any that
are already there. If that file does not exist yet, create it (and the
`~/.claude` folder, if that is missing too) holding exactly the block below,
with your real absolute path and name filled in: the block as printed is
already a whole valid settings file. Write the database path in full; `echo
~/.local/share/ledger/work.db` prints it:

```json
{
  "env": {
    "LEDGER_DB": "/absolute/path/to/work.db",
    "LEDGER_OPERATOR": "me",
    "LEDGER_HARNESS": "claude-code"
  }
}
```

`LEDGER_HARNESS` is `cli` in your shell profile and `claude-code` in the
settings block. The settings value wins over the one a session inherits from
the shell that started it: measured with Claude Code 2.1.328, a session
launched from a shell that exported `LEDGER_HARNESS=cli`, with a settings `env`
block that set `LEDGER_HARNESS=claude-code`, saw `claude-code` from its Bash
tool. So there is nothing to unset before you start Claude Code. Reads such as
`doctor` and `verify` do not need `LEDGER_HARNESS`.

## 6. Make the first write

There is no `init` verb. The first write creates the database file, and the
folders above it, and applies every migration, so there is no folder to make
first:

```sh
ledger new --type ticket --title "My first item" --id first-1
```

It prints one line that starts with `created` and names `first-1`. Run it
only once: a second run with the same id is refused.

With the `ledger-hooks` plugin from step 9 installed, a write can come
before this one. When `LEDGER_DB` names a file that does not exist, the
first prompt you submit in a Claude Code session is passed by the
UserPromptSubmit hook to `ledger capture --kind prompt`, which opens the
database for writing: it creates the file and the folders above it, applies
every migration and stores the prompt as the first event. The question
picker's capture (`ledger capture --kind picker`) does the same. A prompt
the capture skips (a harness frame, an injected prefix, an opted-out folder,
or `capture_prompts` set to `false`) creates nothing, and neither does the
session-start hook. If you want your own first item to be the first event,
make this write before you start a session with the plugin.

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
prints `already claimed for work` and writes no event; running it with another
name is refused, so two systems cannot end up in one database. A system name is
a lowercase letter followed by lowercase letters, digits, `_` or `-`, up to 64
characters.

The command opens the database for writing, so the first write after an
upgrade applies the pending migrations, and a repeat run does too: on a
database from an older release, a run that printed `already claimed` moved
`schema_version` from 9 to 10. An older copy of the ledger then refuses to
open the file, so upgrade every copy that writes to it before this step (the
upgrade page's [section 3.1](upgrading.md#31-the-ledger) says how).

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
search: current
```

`N` is the newest migration applied. `doctor` brings the optional search
index up to date before it reports it; where it cannot write the index it
reports what it finds instead (`missing`, or `behind` with a count), which
is not a fault. `verify` prints `ok events=` and a count. `doctor` also lists
any claim whose lease has expired, and any claim with no lease stamp older
than 7 days, one line each; a new database has none.

If `doctor` says `database does not exist`, it is looking at a path where
nothing has been written: check `echo $LEDGER_DB` and run step 6. If it says
`pass --db or set LEDGER_DB`, step 4 was not done in this shell.

## 9. Connect Claude Code: the hooks plugin

To have Claude Code print the ledger's brief when a session starts and record
your prompts and question-picker answers, install the ledger's plugin from
Claude Code:

```sh
claude plugin marketplace add https://github.com/smithadifd/ledger.git#v0.5.0
claude plugin install ledger-hooks@ledger-plugins
```

The `#v0.5.0` suffix pins the plugin to that release. An add without the
suffix follows the repository's main branch, which can carry plugin changes
that no release has shipped. After a later release, put that release's tag
where `v0.5.0` is.

With v0.5.0, the version this page pins, the hooks and the `ledger` command
they run read `LEDGER_DB` and `LEDGER_OPERATOR` from the environment Claude
Code starts with: the `env` block of its settings (step 5), which also carries
`LEDGER_HARNESS=claude-code`. The session-start hook backs that up: it stamps
`LEDGER_HARNESS=claude-code` when the inherited value is unset or exactly
`cli`, and keeps any other value. The hooks do nothing until `LEDGER_DB` is
set there or the `ledger_db` plugin option names a database.

A database can also be named as the `ledger_db` plugin option. An already-set
`LEDGER_DB`, `LEDGER_OPERATOR` or `LEDGER_HARNESS` is kept, and a plugin
option fills only an unset one. The options are set with
`claude plugin configure ledger-hooks@<marketplace> --values-stdin` and a JSON
object whose keys are `ledger_db` (absolute path), `ledger_operator`, and
`ledger_harness` (optional).

Inside a Claude Code session that has the plugin enabled, `ledger` is on the
session path from the plugin's `bin/`, and each hook runs it; otherwise a hook
runs the first `ledger` on the `PATH` Claude Code starts with. Pipx (step 3)
remains the way to get the command in a terminal.

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
