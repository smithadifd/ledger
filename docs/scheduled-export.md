# A scheduled export without the toolkit

The export is the ledger's durable copy of its events, and it only helps if it
runs. This page sets up two jobs that keep it current, with nothing but the
`ledger` command, `git`, `/bin/sh` and your own scheduler (launchd or cron): one
writes the events, commits them and then pushes the commit to a private
repository, and one checks that the export has not fallen behind. Do the steps
in order. It assumes a working ledger, so do [Start here](start-here.md) first.

## 1. Create a private repository

The events hold your titles, notes and inline documents, so the repository must
be private. Create an empty private repository on your git host and clone it.
With the GitHub CLI:

```sh
gh repo create ledger-export --private --clone
```

With another host, create the empty private repository there and
`git clone` its URL to a folder named `ledger-export`. The clone is the export
folder. Two rules for it:

- Keep it to the export. `export --commit` commits every file in the folder, so
  put the jobs' log files somewhere else.
- It needs a branch that knows where to push. A clone of an empty repository
  sets that up. If you built the folder with `git init` and `git remote add`
  instead, run `git -C ledger-export config branch.main.remote origin` (for a
  branch named `main`); without it, `--push` is refused with
  `export_push_unconfigured`.

## 2. Set the environment

Every job needs two variables:

```sh
export LEDGER_DB=~/.local/share/ledger/work.db
export LEDGER_EXPORT_DIR=~/ledger-export
```

`LEDGER_DB` is the database from step 4 of Start here; `LEDGER_EXPORT_DIR` is
the clone from step 1. Even `--push`, which never opens the database, stops with
`pass --db or set LEDGER_DB` without the first. The jobs do not need
`LEDGER_HARNESS`: `export` reads the database and never writes to it.

Run the three commands once by hand before scheduling them:

```sh
ledger export --commit
ledger export --push
ledger export --check
```

They print, in order (the commit and the counts are yours):

```text
exported 2 watermark=2 commit=66ac6940070cb8c7cd00b83a9e21cbf98d53ab43
pushed 66ac6940070cb8c7cd00b83a9e21cbf98d53ab43 to refs/heads/main at origin
ok lag events=0 hours=0.0 watermark=2
```

## 3. What each command does

- **`ledger export --commit`** writes every event after the watermark into the
  folder and makes one git commit there. It never pushes. With nothing new it
  prints `exported 0 watermark=N commit=-` and commits nothing.
- **`ledger export --push`** pushes the branch's current commit to its push
  remote. It writes no events and makes no commit, so it sends whatever the
  last `--commit` made; run with nothing new, it pushes the same commit again
  and exits 0. It must run after the `--commit` it depends on, which is why the
  export job below runs both in one command.
- **`ledger export --check`** compares the database with the export folder. It
  prints `ok lag events=N hours=H watermark=N` and exits 0, or, when more than
  100 events or 24 hours are unexported, prints
  `export lag N events, H hours (limits 100 events, 24.0 hours)` and exits 3.
  `--max-events` and `--max-hours` change the limits. It does not look at the
  remote, so it cannot see a push that has stopped working.

The scheduler only records the exit status of a job. Nothing on this page sends
an alert: read each job's log, and look at the check job's exit status in your
scheduler, to learn that something is wrong. Run each command once by hand
first; a refusal prints its code, and [docs/cli.md](cli.md) lists them all.

A scheduler gives its jobs a short `PATH`. The job files below name `ledger` by
absolute path (`command -v ledger` prints yours); the ledger also runs `git`, so
if `git` is not in `/usr/bin`, add a `PATH` entry to the job. The push must
also sign in without a prompt, with an SSH key that needs no passphrase entry
or a stored credential.

## 4. Schedule the jobs

Replace each `/path/to/...` with an absolute path: `ledger` is the program, the
database is `LEDGER_DB`, the clone is `LEDGER_EXPORT_DIR`, and the logs go in a
folder of your own that is not the clone. Create the log folder first.

There are two jobs. The export job runs `ledger export --commit` and then,
only if that succeeded, `ledger export --push`, joined by `&&` in one
`/bin/sh -c` command. Commit and push share one job because a push that runs
before its commit leaves the remote behind while `--check` still reports no lag;
in one job the push cannot start first, and a failed commit stops it. The check
job only reads the export folder's watermark, so it may run at any time. If a
path contains a space, put quotes around it inside the command.

### launchd

One property list per job, each running every hour (`StartInterval` is in
seconds). Neither job depends on the other's timing. Save each as
`~/Library/LaunchAgents/<Label>.plist`, check it with `plutil -lint <file>`,
and load it yourself:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.ledger-export.plist
```

To stop a job: `launchctl bootout gui/$(id -u)/local.ledger-export`.
Nothing on this page loads, installs or edits a job for you.

`local.ledger-export.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.ledger-export</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>/path/to/ledger export --commit &amp;&amp; /path/to/ledger export --push</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LEDGER_DB</key>
    <string>/path/to/work.db</string>
    <key>LEDGER_EXPORT_DIR</key>
    <string>/path/to/ledger-export</string>
  </dict>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>StandardOutPath</key>
  <string>/path/to/logs/ledger-export.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/logs/ledger-export.log</string>
</dict>
</plist>
```

`local.ledger-export-check.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.ledger-export-check</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/ledger</string>
    <string>export</string>
    <string>--check</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LEDGER_DB</key>
    <string>/path/to/work.db</string>
    <key>LEDGER_EXPORT_DIR</key>
    <string>/path/to/ledger-export</string>
  </dict>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>StandardOutPath</key>
  <string>/path/to/logs/ledger-export-check.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/logs/ledger-export-check.log</string>
</dict>
</plist>
```

### cron

Run `crontab -e` and add one line per job. Each sets the same two variables
inline and runs once an hour. The export line wraps both commands in
`/bin/sh -c '...'` so that the variables reach both of them and the push runs
only after a successful commit:

```text
0 * * * * LEDGER_DB=/path/to/work.db LEDGER_EXPORT_DIR=/path/to/ledger-export /bin/sh -c '/path/to/ledger export --commit && /path/to/ledger export --push' >> /path/to/logs/ledger-export.log 2>&1
30 * * * * LEDGER_DB=/path/to/work.db LEDGER_EXPORT_DIR=/path/to/ledger-export /path/to/ledger export --check >> /path/to/logs/ledger-export-check.log 2>&1
```

## 5. Check that the pushed copy restores

After the first push, and again now and then, prove that what is in the private
repository restores. Clone it to a fresh temporary folder and restore into a new
database there, never over your real one (`restore` refuses a database path
that already exists):

```sh
CHECK=$(mktemp -d)
git clone "$(git -C ~/ledger-export remote get-url origin)" "$CHECK/export"
ledger --db "$CHECK/restored/work.db" restore --dir "$CHECK/export" --system work
ledger --db "$CHECK/restored/work.db" verify
```

Use the system name you claimed in step 7 of Start here. A healthy result
exits 0 and prints:

```text
restored 2 events into /path/to/the/temporary/folder/restored/work.db
ok events=2
```

The count is yours. It equals the `ok events=N` that `ledger verify` prints for
your live database, or is lower by the events not yet exported. A different
system name is refused (exit 3) rather than restored. Delete `$CHECK` when you
are done.

## 6. Copy the by-path document directory yourself

A document added with `doc add --by-path` is not stored in the database or the
export. The event holds its path under the *document root*, its sha256 and its
size, and the file stays in that root: the folder beside the database named
after it plus `.documents`, so `work.db.documents` beside `work.db`. The export
and `ledger backup` carry the paths and digests, not the bytes. Copy the root by
other means (a file copy, `rsync`, your backup tool); the ledger never writes
under it, and neither job above copies it. Documents added without
`--by-path` are inline and are in the export. A ledger with no by-path
documents has no root to copy.

A restored by-path document looks for its file under the restored database's own
root, named after it. Until the copied tree is there, `doc verify` reports it
`missing` and exits 4. Continuing the check above, with your own root's path:

```sh
ledger --db "$CHECK/restored/work.db" doc verify
cp -R ~/.local/share/ledger/work.db.documents "$CHECK/restored/work.db.documents"
ledger --db "$CHECK/restored/work.db" doc verify
```

The first run prints, with the event id and item id of your own document:

```text
doc verify failed: 1 of 1 documents
3	missing	first-1
```

and the second, once the tree is beside the restored database, hashes every file
against its stored sha256 and prints `ok documents=1` (your count) and exits 0.
