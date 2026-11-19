# Upgrading across many releases

This page is for a machine whose ledger is 0.1.8 or older, moving to the
current release, 0.4.0. Four changes in between can stop a machine that works
today, and 0.2.0, 0.2.1, 0.3.0 and 0.4.0 add a few more things to check. Do the
checks in section 1, install the tag in section 2, run the three upgrade
commands in section 3, and run the check after each. A machine already on
0.1.22, 0.2.0 or 0.2.1 has already passed the changes in 1.1 to 1.4 and needs
only 1.5 and 1.6; a machine on 0.3.x needs only 1.6. For a
later tag, change the tag in the commands and read the
[CHANGELOG](../CHANGELOG.md) entries after 0.4.0.

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

### 1.5 What 0.2.0, 0.2.1 and 0.3.0 add

None of the three changes the database schema or needs a migration. Each item
below applies only to a machine that does the thing it names; check the ones
that fit yours. The [CHANGELOG](../CHANGELOG.md) entries have the detail.

- **A scheduled export (0.2.1).** Before 0.2.1 the credential scan covered
  attached documents, not the text of other events. Since 0.2.1 `ledger export`,
  `export --commit` and `export --push` scan every event's text. An event
  already in the ledger, or already committed in the export repository, whose
  text holds a credential shape is now refused by name (`export_doc_credential`,
  exit 3), where 0.2.0 exported and pushed it. If you run a scheduled export,
  run `ledger export --commit` and then `ledger export --push` once by hand
  after upgrading, before the next scheduled run, with the same `--dir` or
  `LEDGER_EXPORT_DIR` your job uses. An export repository whose history already
  holds such an event file refuses `--push` until that file is removed from its
  history with git; rotate the credential if it was pushed before
  ([scheduled-export.md](scheduled-export.md)).
- **Picker capture reads `config.json` (0.3.0).** This applies if a hook runs
  `ledger capture --kind picker`, as the `ledger-hooks` plugin does. Picker
  capture now refuses with `capture_config` (exit 3) and writes nothing when the
  `config.json` beside the database cannot be read or is not a JSON object, or
  when its `capture_optout_paths` is not an array of absolute path strings.
  Before, it ignored that file. Check that the file, if you have one, is a JSON
  object:

  ```sh
  python3 -c 'import json,sys;assert isinstance(json.load(open(sys.argv[1])),dict)' "$(dirname "${LEDGER_DB:?}")/config.json"
  ```

  The command exits 0 when the file is a JSON object. If it fails with
  `FileNotFoundError`, there is no file and nothing to fix; any other failure
  means the file is unreadable or not an object. It does not check
  `capture_optout_paths`, so read that key yourself if the file has it.
- **Output of `ledger resume --brief` (0.2.0 and 0.3.0).** The text gains a
  "Finding things" line in 0.2.0, and since 0.3.0 its first line is the ledger
  version, exactly what `ledger --version` prints. With `--json`, `--brief` adds
  `version` (the bare number, such as `0.3.0`). Anything that matches the output
  from its first character needs updating.
- **Hand-written `also at` notes (0.2.0).** For an item whose source is
  `row:<ID>`, `ledger export-item --ticket` takes the locator only from a note
  whose idempotency key starts with `row-item:also-at:`, the notes the roadmap
  importer and `row-item.sh ensure` write. A note written by hand, for example
  with `ledger note`, no longer counts. With no keyed note the command refuses
  `row_source_no_also_at`, or prints the item's spec when it has one. Fix:
  re-create the note through the importer or `row-item.sh ensure`, or give the
  item a spec.
- **Writes, `backup` and `restore` check what the path names (0.3.0).** A write
  is reported only after the database path is checked to still name the files
  it was written to. If another file was put at the database's name, or at its
  `-wal` or `-shm` name, the write is refused with `db_swapped` or
  `db_sibling_unsafe` (exit 3). The write did land; the CHANGELOG entry says
  where. `backup` now refuses a destination that is the live database or one
  of its side files, told by file identity and not by spelling, so a hard link
  or a differently spelled name for the live file is refused; `restore` checks
  its destination by file identity too. Both refuse a destination whose name
  ends in `-wal`, `-shm` or `-journal`. Check the destinations your scripts
  give `backup --to` and `restore`. A backup destination may hold a `FILE.partial` left by an older
  version, or a `.FILE.<random>.partial` left by a killed backup; either is safe
  to delete when no backup is running.
- **Library callers (0.3.0).** `ledger.dbfile.checked_connect` returns
  `(conn, mode, proof)` and `ledger.store.connect` returns `(conn, proof)`;
  `Ledger(conn, path, proof)` takes the proof. A caller that unpacked two values
  from `checked_connect`, or used the return of `connect` as a connection, must
  unpack the new shape. `open_ledger` and `open_ledger_readonly` are unchanged.
- **The `ledger-hooks` plugin (new in 0.2.0, optional).** Installing it is
  optional and changes nothing until you do. Up to 0.3.x its hooks ran the
  first `ledger` on the `PATH` Claude Code gives it, so the `ledger` CLI had to
  be upgraded first. From 0.4.0 each hook runs the plugin's own bundled
  `bin/ledger` when `CLAUDE_PLUGIN_ROOT` is set and `bin/ledger` there is
  executable, and only otherwise the first `ledger` on `PATH`; with neither,
  the hook does nothing. Updating the plugin therefore updates the CLI its
  hooks use, and the pipx `ledger` stays the one you run in a terminal. The
  bundled command needs Python 3.11 or later on the `PATH` and does not fall
  back to the pipx command when it cannot find one. Its manifest
  names a `version`, the release number, and Claude Code takes a plugin's
  version from that field: an update replaces your installed copy only when a
  release changes it. (Before the manifest named a version, the plugin
  followed the repository's commits.) If you installed it with 0.2.0 or
  0.2.1, update it (the same two steps as section 3.2, with the marketplace
  `ledger-plugins` and the plugin `ledger-hooks@ledger-plugins`; a marketplace
  added with a pin needs the re-pin described in 1.6 instead): 0.3.0 adds its PostModelSwitch hook, which needs Claude Code v2.1.251 or
  later. Add the marketplace with the pinned command:

  ```sh
  claude plugin marketplace add https://github.com/smithadifd/ledger.git#v0.4.0
  ```

  The `#vX.Y.Z` suffix pins the plugin to that release, and an add without it
  follows main. If you added `ledger-plugins` without the suffix, remove it
  (`claude plugin marketplace remove ledger-plugins`) and install it again with
  the pin. A marketplace added with a pin stays on that tag:
  `claude plugin marketplace update ledger-plugins` re-fetches the same tag, so
  to move to a later release, remove the marketplace and install it again with the
  new tag. If the skills plugin's session-start hook is also installed with an
  explicit `ledger_cli`, a session prints the brief twice, and a separate
  prompt-capture hook records each prompt twice.

### 1.6 What 0.4.0 adds

None of the changes below alters the database schema or needs a migration.
Each item applies only to a machine that does the thing it names; check the ones
that fit yours. The [CHANGELOG](../CHANGELOG.md) entry has the detail.

- **The `ledger-hooks` plugin runs its own `ledger` (0.4.0).** This applies if
  the plugin is installed. From 0.4.0 each hook runs the plugin's bundled
  `bin/ledger` when `CLAUDE_PLUGIN_ROOT` is set and that file is executable,
  and otherwise the first `ledger` on the `PATH` Claude Code starts with. Before
  0.4.0 it was always the `PATH` command. Updating the plugin now updates the
  CLI its hooks use, while the pipx `ledger` stays the terminal command, so
  upgrade both. The bundled command needs Python 3.11 or later on the `PATH`
  and does not fall back to the pipx command when it finds none. A plugin
  installed from a pinned marketplace stays on its old tag until you re-pin
  it: `claude plugin marketplace update ledger-plugins` re-fetches the same
  tag. Remove the plugin and the marketplace, add the marketplace at the new
  tag, install the plugin, then restart the session:

  ```sh
  claude plugin uninstall ledger-hooks@ledger-plugins
  claude plugin marketplace remove ledger-plugins
  claude plugin marketplace add https://github.com/smithadifd/ledger.git#v0.4.0
  claude plugin install ledger-hooks@ledger-plugins
  ```

  The same plugin also takes the `ledger_db`, `ledger_operator` and
  `ledger_harness` options from 0.4.0; an already-set environment value wins,
  so an existing `env` block keeps working unchanged. Prove the re-pin:
  `claude plugin list` shows `ledger-hooks@ledger-plugins` at version 0.4.0.

- **`backup --to` refuses an existing destination (0.4.0).** Before 0.4.0,
  `ledger backup --to FILE` replaced whatever file sat at FILE and exited 0.
  Now a FILE that already exists is refused with `backup target already exists`
  (exit 3) and left untouched. Fix: give each backup a new file name, or add
  `--overwrite` to replace the file as before. A script that writes every
  backup to one fixed path starts failing from its second run. Find the calls,
  with FOLDER your scripts folder:

  ```sh
  grep -rn 'backup --to' FOLDER
  ```

- **New text holding control characters is refused (0.4.0).** A write is
  refused, exit 3, and nothing is written, when the text of a note
  (`item.note`), a progress entry (`item.progress`) or a spec body or
  acceptance (`item.spec`) holds a control character other than line feed and
  tab, or the line or paragraph separators U+2028 and U+2029, or a
  bidirectional control (U+202A to U+202E, U+2066 to U+2069); or when an item
  title or spec title holds any of those or a line feed or tab. The refusal is
  `display_control_character:<kind>.<field>`, for example
  `display_control_character:item.note.text` or
  `display_control_character:item.spec.body_md`. A carriage return adds the
  hint `U+000D: convert line endings to LF`, so a `ledger spec set
  --body-file FILE` whose file has Windows (CRLF) line endings is refused.
  Fix: convert the file to LF line endings (`tr -d '\r' < FILE > NEW`, then
  pass NEW), and remove the control characters from text a script passes.
  Rows written before 0.4.0 still verify, export and restore; the item
  printers now show their control characters as escapes such as `\u001b`.
  Find the spec calls that read a file:

  ```sh
  grep -rn 'body-file' FOLDER
  ```

- **`link --rel pr` checks its target (0.4.0).** The target must be
  `owner/repo#N`, `#N` or `https://github.com/owner/repo/pull/N`, with N a
  number that does not start with 0. Anything else is refused with the code
  `bad_pr_target` (exit 3), and nothing is written; before 0.4.0 any text was
  accepted. Find the calls:

  ```sh
  grep -rn 'link .*--rel pr' FOLDER
  ```

- **A repeated `new --source` prints `exists` (0.4.0).** The second call
  returns the same item and writes nothing, as before, but its text line
  starts with `exists` instead of `created`, and `--json` gives `result` as
  `exists`. The first call still prints `created`. A script that matches
  `created` to learn the item is there must accept `exists` too. Find the
  scripts that call `new` with `--source`:

  ```sh
  grep -rn -e '--source' FOLDER
  ```

## 2. Which tag to install

Install `@v0.4.0`. Never pin 0.1.14. It shipped with a descriptor check that
compared the whole process's open descriptors before and after each connect, so
another thread's open in that window, or SQLite reusing a descriptor it already
held, refused a valid connect (`db_fd_unknown`, and sometimes `db_not_regular`,
`db_swapped` or `db_stat_failed`). 0.1.15 replaced the check
([CHANGELOG](../CHANGELOG.md): "upgrade to 0.1.15 rather than vendoring or
pinning 0.1.14").

## 3. The three upgrade commands

Run them in this order. Each has a check that proves it.

`claude plugin install` (and `claude plugin update`) rewrites
`~/.claude/settings.json`. If you also edit that file, for example to add the
`env` block, do the plugin installs first and the edit after, or open the file
again and re-read it right before you edit it, so the edit does not start from a
copy the install has since changed.

### 3.1 The ledger

First check how this machine installed the ledger. Run:

```sh
command -v ledger
type -a ledger
```

`command -v` shows the copy that runs; `type -a` (bash or zsh) or `which -a`
shows every copy. If the path ends `.../bin/ledger` inside a virtual
environment folder that is not pipx's, the ledger was installed with that
venv's pip, and the pipx line below would add a second install, so PATH order
would decide which one runs. Choose one:

- Upgrade with that venv's own pip, and skip the pipx line. `<venv>` is the
  folder that holds that `bin/ledger`:

  ```sh
  <venv>/bin/pip install "git+https://github.com/smithadifd/ledger.git@v0.4.0"
  ```

- Or remove that venv, then run the pipx line below.

Either way, `command -v ledger` then shows one path and `ledger --version`
prints `ledger 0.4.0`.

If `command -v ledger` prints nothing, the ledger you use is not on `PATH`: a
vendored copy, or a wrapper script that runs one from a folder of its own.
Neither choice above fits it. Build a new one beside it and move every caller
over. This route replaces the pipx command below: do not run both, or the
machine ends with two installs and `PATH` order decides which one runs. After
step 3, skip the `pipx install` line and go on to the checks after it.

1. Build a virtual environment on a Python that passes the 1.1 probe (`PY` is
   that Python; `<venv>` is a folder of your choosing) and install the tag into
   it:

   ```sh
   "$PY" -m venv <venv>
   <venv>/bin/pip install "git+https://github.com/smithadifd/ledger.git@v0.4.0"
   <venv>/bin/ledger --version
   ```

   The last line prints `ledger 0.4.0`.
2. Link it onto `PATH`, in a folder that is already on it (`~/.local/bin` for a
   pipx user):

   ```sh
   ln -s <venv>/bin/ledger ~/.local/bin/ledger
   command -v ledger
   ```

3. Before the new copy writes anything, list every caller of the old copy: its
   wrappers, scheduled jobs, hook and plugin settings (for example a
   `ledger_cli` option), scripts and agent instructions. Search each folder
   that holds them (`FOLDER`) for the old copy's path:

   ```sh
   grep -rn 'OLD-COPY-PATH' FOLDER
   ```

   Point each caller at the new `ledger`, or stop it. The new copy migrates the
   database on its first write, and from then on the old copy cannot open the
   file: it refuses a database whose schema is newer than it knows
   ([migrations.md](migrations.md)). `ledger claim-system NAME` counts as a
   write here, even on a repeat.

```sh
pipx install --force --python "$PY" git+https://github.com/smithadifd/ledger@v0.4.0
```

This is the [README's upgrade command](../README.md#upgrade) with the Python you
probed in 1.1 named, so pipx builds the ledger on that Python and not another.
Prove it:

```sh
ledger --version
ledger doctor
```

`ledger --version` prints `ledger 0.4.0`. `ledger doctor` opens the database
that `--db` or `LEDGER_DB` names, so it also proves 1.1 and 1.2 on the real
file: it exits 0 and prints the path, the schema version and the search-index
state. A refusal from section 1 shows up here. If your shell says `ledger:
command not found`, run `pipx ensurepath` and open a new terminal.

### 3.2 The skills plugin

From the [skills repository](https://github.com/smithadifd/skills)
(its changelog's 0.12.0 "Upgrading" steps), first refresh its marketplace
with `claude plugin marketplace update skills`, then update the plugin:

```sh
claude plugin update sdlc@skills
```

Then start a new session, or run `/reload-plugins` in an open one. If you
installed with `install.sh` instead, pull your clone of that repository and run
`./install.sh --all` again. The changelog's steps also add the `env` block the
plugin's session-start hook uses.

Prove it: `claude plugin list` shows `sdlc@skills` and its version, which is
the newest in that repository's `CHANGELOG.md`. With `ledger_cli` set, a new
session's first lines show `ledger 0.4.0` and the ledger's brief, or one
`ledger session-start: ...` line naming what failed.

### 3.3 The ledger mod

From the [mods repository](https://github.com/smithadifd/mods):

```sh
claude plugin update ledger
```

Then restart the session. Prove it: `claude plugin list` shows `ledger@mods` and
its version, which is the newest in that repository's `CHANGELOG.md`, and
`/ledger` opens its pane in a new session. The pane reads the ledger with
`--json`; it needs ledger 0.1.5 or newer, so it needs nothing more of 0.4.0.
