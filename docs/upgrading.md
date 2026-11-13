# Upgrading across many releases

This page is for a machine whose ledger is 0.1.8 or older, moving to the
current release, 0.1.22. Four changes in between can stop a machine that works
today. Do the four checks in section 1, install the tag in section 2, run the
three upgrade commands in section 3, and run the check after each. For a later
tag, change the tag in the commands and read the [CHANGELOG](../CHANGELOG.md)
entries after 0.1.22.

To see which release you have, run `pipx list`. `ledger --version` only exists
from 0.1.16; on an older release it fails with `the following arguments are
required: verb`.

The upgrade does not touch your database. The next write applies any migration
it lacks ([migrations.md](migrations.md)).

## 1. Before you upgrade

Run each check on the machine you are upgrading. A check that fails is cheaper
to fix now than after the upgrade stops the ledger.

### 1.1 A Python whose SQLite is linked statically (refused since 0.1.15)

Since 0.1.15 the ledger asks SQLite's C library which files a connection holds,
and refuses every open it cannot answer for. A Python build from
python-build-standalone, which uv-managed Python uses, links SQLite statically
and hides those functions.

Check the Python the ledger will run under. `PY` is the one you will give pipx
in section 3; replace the first line with another interpreter's path to probe
that one instead:

```sh
PY=$(command -v python3)
"$PY" -c 'import ctypes,sqlite3,_sqlite3
names="sqlite3_libversion sqlite3_auto_extension sqlite3_cancel_auto_extension sqlite3_file_control sqlite3_limit sqlite3_db_filename sqlite3_filename_wal sqlite3_filename_journal".split()
found=[c for c in ([_sqlite3.__file__] if getattr(_sqlite3,"__file__",None) else [])+[None] if all(hasattr(ctypes.CDLL(c),n) for n in names)]
lib=ctypes.CDLL(found[0]) if found else None
if lib: lib.sqlite3_libversion.restype=ctypes.c_char_p
ok = bool(lib) and lib.sqlite3_libversion().decode()==sqlite3.sqlite_version
print("ok: shared SQLite "+sqlite3.sqlite_version if ok else "REFUSED: the ledger cannot use this Python")
raise SystemExit(0 if ok else 1)'
```

It prints `ok: shared SQLite <version>` and exits 0 when the ledger can use that
Python. It prints `REFUSED: the ledger cannot use this Python` and exits 1 when
it cannot. The probe repeats the ledger's own test: the `_sqlite3` library, or
else the process, must export the eight C functions the ledger calls, and be the
same SQLite as the `sqlite3` module.

After the upgrade, on a refused Python, every verb that opens a database exits 3
and nothing is written (`ledger --version` opens none, so it still works):

```text
db_fd_unattributable: connection capture unavailable: SQLite library unavailable: SQLite C API not found (...). The usual cause is a Python build from python-build-standalone ... Use a Python whose sqlite3 module links a shared libsqlite3, such as a distro build: <database path>
```

Fix: pick a Python 3.11 or newer whose `sqlite3` module links a shared
libsqlite3, such as a distro build; set `PY` to it, run the probe again, and
install with `--python "$PY"` (section 3).

### 1.2 A database path whose last component is a symbolic link (refused since 0.1.14)

```sh
echo "$LEDGER_DB"
ls -ld "${LEDGER_DB:?}"*
```

`ls -ld` lists the database and any `-wal`, `-shm` or `-journal` file beside
it. A line that starts with `l` is a symbolic link. Check every place that names
the database: `LEDGER_DB` in your shell profile, `--db` in scripts and
scheduled jobs, the `env` block of your Claude Code settings, and the mod's `db`
option. Only the last component matters; a symbolic link in a folder above the
database is fine.

After the upgrade, a database path that is a link exits 3:

```text
db_symlink: database path is a symbolic link: <path>
```

With `--json` the error code is `db_symlink`. A link where a `-wal`, `-shm` or
`-journal` file should be is refused the same way as `db_sibling_unsafe`.

Fix: point `LEDGER_DB`, `--db` and the mod's `db` option at the real file. This
prints it:

```sh
python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$LEDGER_DB"
```

### 1.3 `ledger pick` and recorded grants (changed in 0.1.15)

`ledger pick` takes only `ITEM --ask K` since 0.1.15: the owner's answer comes
from the picker answer the harness captured, never from the caller. A grant
now stands only when it is the bound answer to an ask that carries its grant
id. A grant recorded before 0.1.15, such as one recorded with `ledger elect`
quoting a typed prompt, does not stand, because no picker capture exists for it.

Check your callers. Run both from each folder that holds your scripts,
scheduled jobs, hook settings or agent instructions (`FOLDER`):

```sh
grep -rnw pick FOLDER
grep -rn -e '--question' -e '--option' -e '--chosen' -e '--free-text' FOLDER
```

A `ledger pick` line that carries any of `--question`, `--option`, `--chosen`,
`--free-text` or `--scope` is the retired form. Read past a trailing backslash:
the grep shows only the first line of a call.

Record the grants that stand today, so you can ask for them again afterwards.
This works on the old release too:

```sh
ledger grants > grants-before.md
```

Each row with status `STANDING` reads `UNVERIFIED` after the upgrade.

After the upgrade:

- The retired form exits 3: `pick needs --ask EVENT: it records the harness's
  capture of the owner's answer to that ask (the --question/--option/--chosen
  form is retired)`, code `pick_needs_ask`. A call that has `--ask` and a
  retired flag is a usage error (exit 2).
- `ledger grants` lists the old grants as `UNVERIFIED`. `ledger grants --check
  GRANT_ID` exits 3 with `grant_unverified` and the reason in parentheses, such
  as `pre_binding` or `grant_unasked`. A withdrawn grant stays withdrawn.

Fix: change each `pick` call to `ledger pick ITEM --ask K`, and raise each
grant you still want as a new ask whose question id is the grant id. The owner
answers it in the picker, the harness hook records the answer with `ledger
capture`, and `ledger pick ITEM --ask K` records the election. "Answers that
open a gate" in [concepts.md](concepts.md#answers-that-open-a-gate) has the
flow and every reason.

### 1.4 Abbreviated long options (refused since 0.1.16)

Before 0.1.16 the ledger accepted any unambiguous prefix of a long option, so
`--d` meant `--db`. Since 0.1.16 only the full name works.

Check your callers. List the calls in each folder that holds your scripts,
scheduled jobs, hook settings or agent instructions (`FOLDER`):

```sh
grep -rnE '(^|[[:space:];&|(])ledger[[:space:]]+(-|[a-z])' FOLDER
```

Read each long option on those lines against `ledger VERB --help`, which lists
every option spelled in full. Read past a trailing backslash here too.

After the upgrade, an abbreviated option exits 2. After the verb the message is
`unrecognized arguments: --js`. Before the verb it is misleading: the unknown
option is skipped and the next word is read as the verb, so `ledger --d PATH
doctor` says `argument verb: invalid choice: 'PATH' (choose from ...)`.

Fix: spell the option in full.

## 2. Which tag to install

Install `@v0.1.22`. Never pin 0.1.14. It shipped with a descriptor check that
compared the whole process's open descriptors before and after each connect, so
another thread's open in that window, or SQLite reusing a descriptor it already
held, refused a valid connect (`db_fd_unknown`, and sometimes `db_not_regular`,
`db_swapped` or `db_stat_failed`). 0.1.15 replaced the check
([CHANGELOG](../CHANGELOG.md): "upgrade to 0.1.15 rather than vendoring or
pinning 0.1.14").

## 3. The three upgrade commands

Run them in this order. Each has a check that proves it.

### 3.1 The ledger

```sh
pipx install --force --python "$PY" git+https://github.com/smithadifd/ledger@v0.1.22
```

This is the [README's upgrade command](../README.md#upgrade) with the Python you
probed in 1.1 named, so pipx builds the ledger on that Python and not another.
Prove it:

```sh
ledger --version
ledger doctor
```

`ledger --version` prints `ledger 0.1.22`. `ledger doctor` opens the database
that `--db` or `LEDGER_DB` names, so it also proves 1.1 and 1.2 on the real
file: it exits 0 and prints the path, the schema version and the search-index
state. A refusal from section 1 shows up here. If your shell says `ledger:
command not found`, run `pipx ensurepath` and open a new terminal.

### 3.2 The skills plugin

From the [skills repository](https://github.com/smithadifd/skills)
(its changelog's 0.12.0 "Upgrading" steps):

```sh
claude plugin marketplace update skills
claude plugin update sdlc@skills
```

Then start a new session, or run `/reload-plugins` in an open one. If you
installed with `install.sh` instead, pull your clone of that repository and run
`./install.sh --all` again. The changelog's steps also add the `env` block the
plugin's session-start hook uses.

Prove it: `claude plugin list` shows `sdlc@skills` and its version, which is
the newest in that repository's `CHANGELOG.md`. With `ledger_cli` set, a new
session's first lines show `ledger 0.1.22` and the ledger's brief, or one
`ledger session-start: ...` line naming what failed.

### 3.3 The ledger mod

From the [mods repository](https://github.com/smithadifd/mods):

```sh
claude plugin update ledger
```

Then restart the session. Prove it: `claude plugin list` shows `ledger@mods` and
its version, which is the newest in that repository's `CHANGELOG.md`, and
`/ledger` opens its pane in a new session. The pane reads the ledger with
`--json`; it needs ledger 0.1.5 or newer, so it needs nothing more of 0.1.22.
