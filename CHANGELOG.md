# Changelog

## Unreleased

A write verb now reports a write only once the database path still names the file it was written to.

- **A write is checked against its open before it is reported.** The open check already proved that the connection's descriptors are the files at the database's names; it now keeps that proof. After each write is durable (after its `COMMIT`, and for `claim-system` after the checkpoint that settles the claim), and after `open_ledger` migrates, the ledger checks the main file, `-wal`, `-shm` and the main file again: each name must still be a regular, singly linked file that is the proved one, and the connection must still hold the proved descriptor. If another file was put at one of those names, for example by a `backup --to` aimed at the live database's path, the write is refused with `db_swapped` or `db_sibling_unsafe` (exit 3) instead of reporting success. The write did land. When the main file was swapped, it is in a file the path no longer names, so it is not in the database at that path. When only the `-wal` or `-shm` name was swapped, the refused connection's close checkpoint writes the write into the main file, which still names the proved database, so it is in the database at that path; the ledger does not retry or roll it back either way. Each database-writing verb, `doc add` included, checks after its own commit. `restore` checks after the commit of its replay, so a restore of an export that already carries a system claim is checked too, and after its later writes (the claim of an unclaimed export, the receipt of a loop-managed export). The guard accepts nothing it refused before.
- **A refused write leaves the `-wal` and `-shm` names alone.** When the check refuses, the connection is set to keep both names when it closes (SQLite's `SQLITE_FCNTL_PERSIST_WAL`). SQLite's close otherwise unlinks both names, whatever files they hold by then, so a refusal caused by another file at one of them, such as a backup published there, would go on to delete that file. This holds for the ledger's own connection, `restore`'s and `backup`'s source connection. A file left on a `-wal` or `-shm` name survives the refused close only until the ledger next opens that database: the next last close of an ordinary connection removes it. A file moved onto a live `-wal` or `-shm` name by anything other than a ledger verb is still removed by a later last closer that is not the refused connection (see KNOWN-LIMITS.md).
- **A refused restore unlinks nothing.** When a restore is refused with one of those codes for the destination or its `-wal` or `-shm` name, the files at those names are no longer the ones it wrote, so it leaves them in place and names them in the refusal. Any other restore failure, including one of those codes about another path, removes the restore's own files as before.
- **`backup` and `restore` refuse a destination named like a database side file.** A destination whose name ends in `-wal`, `-shm` or `-journal` is refused by name alone, before anything is written, because SQLite removes a file on those names when the last connection to a database closes. `backup` keeps its earlier refusal of its own source's side names first, with its own message.
- **`backup` and `restore` tell a destination from the live database by file identity, not by spelling.** macOS's default volume ignores case and Unicode normalization, so a destination spelled `LEDGER.db`, or with a decomposed accented letter where the live database's name has a composed one, can be the live file; comparing names missed the second, and `backup` published its copy over the live database, losing the events committed while it ran. A hard link is another name for the same file on any volume. `backup` now refuses a destination that exists and is the live database, its `-wal`, `-shm` or `-journal` file (same device and inode, read without following a symbolic link), and still refuses its source's exact side-file names; it runs this check before anything else reads the source. `restore` refuses a destination any file already has, by the same identity read. A destination whose identity cannot be read is refused (`backup target cannot be told apart from the live database`, `restore target cannot be checked`), never published over. The side-file name refusal ignores case only where the destination's folder folds case: on a volume that tells case apart, `copy.db-WAL` and a real sibling `LEDGER.db` are ordinary destinations. The folder is probed by looking up one of its own entries with the case swapped; an empty folder, or one whose entries hold no ASCII letter, is treated as folding, so such a name is still refused there.
- **`backup` writes its copy at a unique name and checks its source before publishing.** The copy is written at `.FILE.<random>.partial` beside FILE, created readable by its owner only, instead of the fixed `FILE.partial`, so two backups to the same FILE no longer delete each other's copy; the published file keeps those owner-only permissions. Before the copy is renamed into place, the source path must still name the file it was read from (`db_swapped` otherwise), and nothing is published. A `FILE.partial` left by an older version is no longer removed by the next backup. A backup killed mid-run (by a signal or a power loss) leaves its hidden `.FILE.<random>.partial` behind, and no later backup removes it.
- **Two simultaneous first claims of the same name both succeed.** Two `claim-system NAME` callers on a new database each checkpoint the database after their commit, and one checkpoint could find the other's still running; that caller was refused `shared_database` (exit 3) although the claim was recorded. The verb now reads the claim back on the connection it opened and retries the checkpoint, up to 5 attempts started within 1 second, after a checkpoint that cannot finish or an I/O error in the claim's transaction. It exits 0 only when the name's claim is on record, a checkpoint finishes and the database path still names the files it opened (the same open check every write uses); `claimed` is `true` only when this call's write is known to have committed the claim, so exactly one of two simultaneous callers says `claimed`. A claim on record whose checkpoint is still held when the budget is spent, a read-back or checkpoint that fails, or a name not on record is still refused `shared_database`, and a swapped path `db_swapped` or `db_sibling_unsafe`. `claim_database` and `restore` are unchanged: they still raise on the first checkpoint that cannot finish. Library: `Ledger.claim_system` raises `UnsettledClaimError`, a `SharedDatabaseError`, for those two failures, and `Ledger.claim_outcome(system, refusal)` settles it or raises it again.
- **Library: `checked_connect` and `connect` return the proof.** `ledger.dbfile.checked_connect` returns `(conn, mode, proof)`, `ledger.store.connect` returns `(conn, proof)`, and `Ledger(conn, path, proof)` takes the proof; `ledger.dbfile.verify_bound(proof)` runs the check. A caller that unpacked two values from `checked_connect`, or used `connect`'s return as a connection, must unpack the new shape. `open_ledger` and `open_ledger_readonly` are unchanged for callers.

**Upgrading from 0.2.1.** There is no schema change and no migration. A backup destination may hold a `FILE.partial` from an older version, or a `.FILE.<random>.partial` from a killed backup; either is safe to delete when no backup is running.

## 0.2.1 — 2026-11-13

Two fixes to `ledger export --commit` and `--push`: committing no longer throws away what you staged in the export repository, and the credential scan now covers the text of every event, not only attached documents.

- **`export --commit` keeps what you staged in the export repository.** 0.2.0 set the repository's index to the new commit's tree after every commit, which silently dropped anything you had staged there (a staged edit came back as the committed content, and the repository looked clean). The commit step now merges the index from the old head onto the new commit, as `git read-tree -m -i` does: paths the export changed take the new content, and every staged change, staged deletion and staged new file stays staged with the same content. When something staged sits on a path the commit also changed, the index is left exactly as it was and `index_refreshed` is false; the warning now points at `git status` rather than a command that would drop staged changes. `export --push` never touches the index.
- **The export credential scan covers every event's text.** 0.2.0 scanned only `doc.attached` documents, so a credential pasted into a note, an item or spec title, a spec body, a claim owner or any other text field passed `export`, `export --commit` and `export --push` and could reach the remote. Every event kind is now scanned: every text field and key the event file holds, plus a document's bytes or stored path. A hit refuses with the existing `export_doc_credential` (exit 3), naming the event, its item, the field and the shapes, never the text; nothing is written, committed or pushed. Text the scan cannot read (not UTF-8) is refused the same way. `credential_override: true` still lets an attached document through, and only that: it changes nothing on any other event. The ledger never removes an event, so a refused event keeps refusing later exports, and an export repository whose history already holds such an event file refuses `--push` until that file is removed from its history with git (rotate the credential if it was pushed before).

**Upgrading from 0.2.0.** There is no schema change and no migration. If you run a scheduled export, run `ledger export --dir DIR --commit` and then `--push` once by hand after upgrading, before the next scheduled run: an event already in the ledger, or already committed in the export repository, whose text holds a credential shape is now refused by name where 0.2.0 exported and pushed it.

Upgrade with `@v0.2.1`.

## 0.2.0 — 2026-11-13

The repository is now a Claude Code plugin marketplace, `ledger-plugins`, whose one plugin, `ledger-hooks`, prints the ledger's session brief and records prompts and question-picker answers; the release also adds `ledger claim-system`, `export-item --ticket` for a ledger-born ticket and `spec set --expect-revision`, and `export-item --ticket` now takes the also-at locator only from a keyed note.

- **The `ledger-hooks` plugin.** Install it with `claude plugin marketplace add smithadifd/ledger`, then `claude plugin install ledger-hooks@ledger-plugins`. Three small `sh` shims call the ledger CLI and hold no logic of their own. SessionStart appends what `ledger resume --hook-env` prints to the session's environment file (when Claude Code provides one), then prints `ledger resume --brief`. UserPromptSubmit pipes the hook input to `ledger capture --kind prompt`. PostToolUse on `AskUserQuestion` pipes it to `ledger capture --kind picker`, the capture `ledger pick ITEM --ask K` binds. Every shim does nothing, and prints nothing, unless a `ledger` is on `PATH` and `LEDGER_DB` is non-empty, and every shim exits 0 inside its timeout (30 s for the brief, 10 s for each capture). A failing brief prints one line, `ledger-hooks: ledger resume --brief exited N`, into the session; a failing capture leaves one fixed stderr line with no prompt text, which Claude Code shows only in its debug log. Each hook runs the first `ledger` on the `PATH` Claude Code gives it, so a different `ledger` earlier on that `PATH` runs instead and receives your prompts and picker answers. The plugin takes no absolute path and has no setting for one, so `PATH` order is the only control: check that `command -v ledger`, in the environment Claude Code starts with, names the ledger you installed. The plugin sets no version, so an install follows the repository's commits, not this tag: upgrade the `ledger` CLI to 0.2.0 first.
- **Prompt capture skips harness frames and can be turned off.** `ledger capture --kind prompt` now skips a prompt that starts, after ASCII whitespace, with `<task-notification>`, `<cross-session-message`, `<agent-message`, `<system-reminder>` or `[Subagent hand-back]` (exact, case-sensitive), prints `skipped<TAB>frame` and writes nothing. `"capture_prompts": false` in `config.json` beside the database skips every prompt (`skipped<TAB>prompts_off`); picker capture never reads the key. A `config.json` that is unreadable, is not a JSON object, is a symbolic link that does not resolve, or holds a `capture_prompts` that is not a boolean (`"false"`, `0` and `null` included) refuses prompt capture with `capture_config` (exit 3) and writes nothing, so a typo never stores prompts you turned off. Both skips happen before the database is opened. A prompt input that carries `prompt_id`, and every picker input, run twice leaves one capture; a prompt input with no `prompt_id` may be stored twice on a re-run.
- **`ledger resume --hook-env`.** Reads a SessionStart hook input and prints `export NAME=<quoted value>` lines: `LEDGER_SESSION` always, and `LEDGER_AGENT` and `LEDGER_HARNESS` only when that variable is not already set. It stamps no `LEDGER_MODEL`, because the session's environment file cannot be rewritten when the model changes mid-session, so writes from a plugin-started session record no model unless `LEDGER_MODEL` is already in Claude Code's environment. It never resolves, opens or creates the database. Another event's input is refused `hook_env_not_session_start` and unreadable input `hook_env_bad_input` (exit 3, empty stdout); combining it with `--brief` or `--item` is a usage error (exit 2).
- **`ledger claim-system NAME`.** Claims an unclaimed database for the system NAME (for example `work` or `home`), so two systems can keep separate databases without a library call. The same NAME again exits 0, says the database is already claimed and writes no second event; another NAME is refused `shared_database` (exit 3) and writes nothing. A synced or symlinked database path is refused by the checks every write verb uses (`synced_path`, `db_symlink`) and no file is created. NAME is 1 to 64 characters, a lowercase letter then lowercase letters, digits, `_` or `-`; anything else is a usage error (exit 2) raised before the database opens. It needs `LEDGER_HARNESS`, like every writing verb, and the claim event carries the caller's attribution. `claim_database` and `restore_export` take the same keyword-only `actor`, `harness`, `model` and `session` arguments as `Ledger.claim_system`; the restore verb passes the caller's attribution, so a CLI restore of an unclaimed export records it on the `ledger.system` claim (and on a loop-managed export's `session.opened` receipt). Two callers claiming the same name at once both record one claim, but one of two simultaneous first claims on a new database can be refused `shared_database` even though the claim was recorded, and running it again reports the database as already claimed.
- **`export-item --ticket` prints a ledger-born ticket's spec.** An item with a current spec whose source names no file now prints that spec: `spec: vN`, then `title:`, `size:` and `merge_class:` (each only when recorded), a `## Body` section and, when recorded, `## Acceptance`. "Names no file" means an empty source, a source that is neither a locator nor a row key (a follow-up's `PARENT:slug`), or a row key with no valid keyed `also at` note. With `--json`, `data` gains a `spec` object (`version`, `title`, `size`, `merge_class`, `body_md`, `acceptance`; unrecorded fields `null`); the locator case is unchanged and has no `spec` key. A source that does name a file reads and refuses exactly as before: the spec never stands in for a broken locator. An item with no spec keeps its refusals. A spec event that cannot be read is refused `spec_unreadable` (exit 3) and prints no text. This changes 0.1.13 for one case: a row-key item with a spec and no valid keyed note used to be refused `row_source_no_also_at`; it now prints its spec.
- **`ledger spec set --expect-revision ID`.** The flag means what it means for `append`, `state`, `note` and `append-many`: the write goes through only if the item's newest event is still ID. The check runs inside the write transaction, after the not-found check and before the unchanged-content compare, so a stale revision exits 5 (`stale_revision`) and writes nothing, even when the content and source are unchanged. A matching revision with unchanged content is the usual no-op; `none` never matches, because every item has a creation event. A caller can now refuse a spec that would land on a close or an import it never read. A malformed value is refused as `note` refuses it, and `spec show` and `spec diff` refuse the flag. Without the flag nothing changes. The library's `set_spec` takes `expect_revision`.
- **Only a keyed note supplies the also-at locator.** For an item whose source is `row:<ID>`, `export-item --ticket` reads the locator from the newest `also at <locator>` note whose idempotency key starts with `row-item:also-at:` (the namespace the roadmap importer and `row-item.sh ensure` write). A hand-written `also at` note, for example one from `ledger note`, is skipped, and is never a fallback: with no keyed note the command refuses `row_source_no_also_at`, or prints the item's spec when it has one. Before, any note whose text parsed could stand in for the locator and redirect the export. This supersedes the 0.1.13 entry, which said the newest `also at` note supplies the locator.
- **A deeply nested or unparseable export file is refused by name.** `load_export`, `read_watermark` and `verify_stream_digest` now report their own named refusals for such a file instead of letting a `RecursionError` escape.
- **`ledger resume --brief` prints one more line.** The short form of the agent contract gains a "Finding things" line naming `search`, `context`, `links`, `questions` and `decisions`, mirroring a new "Finding things" section of the contract page.
- **Docs and tests.** New pages: `docs/upgrading.md` (upgrading across many releases), `docs/scheduled-export.md` (a scheduled export with only the ledger installed) and `docs/releasing.md` (a release checklist); `docs/start-here.md` gains steps for claiming the database, the plugin and a scheduled export; the README and `docs/cli.md` document the plugin. Test-only changes pin the `questions --since`/`--until` refusal and the card ask-line check.

**Upgrading from 0.1.x.** There is no schema change and no migration. If you wrote `also at` notes by hand to point `export-item --ticket` at a ticket, they no longer count: re-create them through the roadmap importer or `row-item.sh ensure`, or give the item a spec. `resume --brief` output gains a line, so anything that matches it exactly needs updating. Installing `ledger-hooks` is optional and changes nothing until you do; if you do, it adds a second brief and a second copy of the session env lines only when the skills plugin's session-start hook is also installed with an explicit `ledger_cli`, and a separate prompt-capture hook records each prompt twice. The skills-side change that would drop that overlap is not released yet. Otherwise: nothing.

Upgrade with `@v0.2.0`.

## 0.1.22 — 2026-11-12

`ledger export --commit` and `ledger export --push` now run the credential scan on what they send off the machine.

- **`ledger export --commit` and `--push` run the credential scan.** `--commit` stages into a temporary index and scans the exact tree it commits and the new commit object, and moves only `refs/heads/<branch>`, by a compare-and-swap from the head it read, and only if HEAD still names that branch. `--push` scans every commit, path and blob reachable from the pinned branch commit, then pushes exactly that sha to `refs/heads/<branch>` at the branch's push remote, with no tags, no push options and no push certificate (it ignores `push.gpgSign`). Replace refs and grafts are off for both steps, and a shallow repository is refused. A file at an event name must be the canonical bytes of a valid event record; every other file, every path name, every commit object and the branch name must hold no credential shape. `export --push` no longer uses `push.default`, `remote.<name>.push` or a fallback remote, and refuses a detached HEAD and a remote with several push URLs; `export --commit` refuses a detached HEAD and signs its commit when `commit.gpgSign` is true. Every refusal has a named code and exits 3; the codes are listed in `docs/cli.md`. With `--json`, `--push` reports `branch` and `remote` beside `sha`, and `--commit` reports `index_refreshed`.

Upgrade with `@v0.1.22`.

## 0.1.21 — 2026-11-11

By-path documents: `ledger doc add --by-path` stores a document's path under the database's document root instead of its bytes, and `doc show --content` and `doc verify` re-read the file through a no-follow walk from that root on every call.

- **`ledger doc add --by-path` stores a document by path.** `doc add ITEM FILE --role ROLE --by-path` stores FILE's path relative to the database's document root (the directory beside the database file named as the database file plus `.documents`, so `work.db.documents` beside `work.db`) with the sha256 and size of the bytes it read, and not the bytes, for a file of at most 16777216 bytes (16 MiB). Over that limit, or `-` with `--by-path`, is a usage error (exit 2), and an inline add over the 65536-byte limit now names `--by-path` in its usage error. The ledger never creates, writes, moves or deletes anything under the root: put the file there first. FILE is placed under the root component by component, never by string prefix, against the physical root or the root as the database path was spelled (for macOS, where `/tmp`, `/var` and `/etc` are symlinks), and a relative FILE against the physical working directory; anything else is refused `doc_outside_root` (exit 3). The root is opened from the database's directory without following a symlink and the stored path is walked from it with the same no-follow walk an inline add uses; a relative FILE is also walked from the working directory and must reach the same file. FILE naming the root itself, a component spelled otherwise than its directory entry (which a case-insensitive volume would open), and a relative FILE whose two walks do not reach the same file are refused `doc_unreadable`. Adding the same item, role and sha256 again reports `unchanged` whatever the storage.
- **`doc show --content` and `doc verify` re-read a by-path document on every call.** Each walks the stored path from the document root of the database it opened, without following a symlink at the root or below it, and hashes the bytes that walk read; nothing from an earlier walk or add is reused, and only bytes that match the stored sha256 and size are printed or reported `ok`. An absent root, directory or file is `missing` (`doc_missing`), any other walk failure (a symlink, a FIFO or other non-regular file, a platform that cannot open without following symlinks) is `refused` (`doc_refused`), and other bytes are a `mismatch` (`doc_mismatch`), each exit 4; `doc verify` reports those statuses and exits 4 with `doc_verify_failed`. `doc show` without `--content` and the `ledger context` card read events only. In `doc show` text a by-path document's storage column reads `path`, followed by its stored path escaped as a text column; with `--json`, `storage` is `inline` or `path` and `path` holds the stored path. Inline lines are unchanged.
- **The `doc.attached` payload carries exactly one of `content_b64` and `path`.** Both or neither is refused `exactly-one-of:content_b64,path`; a stored path that is absolute, empty, has a trailing or doubled `/`, a `.` or `..` component or a control character, is over 1024 bytes or has a component over 255 bytes is refused `bad-doc-path:path`; and `size` is at most 16777216 for every document (`too-large:size`). These apply at add, restore and replay. Stored inline events keep validating, so no migration is needed.
- **The credential-shape scan covers a by-path document's stored path.** `doc add` scans the bytes it read, inline and by path alike, and for a by-path add also the stored path's UTF-8 bytes, before any file is opened; a hit in either is refused `doc_credential_shape` unless `--allow-credential-shapes` is given, which marks the event `credential_override: true`. Restore and replay scan an inline document's bytes and a by-path document's stored path and refuse an unmarked hit `restore_doc_credential`, inserting nothing. `export` scans a by-path document's stored path and refuses an unmarked hit, or a stored path that is not UTF-8 text, `export_doc_credential`, writing nothing and leaving the watermark where it was. Restore, replay and export open no document file. A hit in a stored path is named by its shape and said to be in the path; the path is never shown. The `--commit` and `--push` steps themselves do not scan: `--commit` stages whatever the export folder holds after a clean export, and `--push` pushes without opening the ledger.
- **Exports and backups hold a by-path document's path, not its bytes.** Its event, the export and `backup --to` carry the stored path, sha256 and size; the bytes live only under the document root, so copy that directory too. A restored by-path document resolves against the restored database's own document root, never the source's, so `doc verify` reports it `missing` until the old root's tree is put beside the restored database, named after it. The README no longer calls the export the durable copy of everything.

Upgrade with `@v0.1.21`.

## 0.1.20 — 2026-11-11

Documents (the `doc.attached` event and `ledger doc add|show|verify`) with a credential-shape scan, card cut sections that carry a count and command in `--json`, `query --open`, one breaking change to `project_item`, and fixes to export, events output and `ledger doctor`.

- **BREAKING: `ledger.card.project_item` takes an item id, not an item row.** Its second argument is now `item_id`; the function reads the row inside the same read snapshot as the events and raises `KeyError` for a missing item, as `project_card` does. A caller that passed a row must pass the id instead and handle `KeyError`. Only tests called it in this repository.
- **`ledger card --full` names the event behind each Blockers row.** The `blocks` link event behind a Blockers row (including when only the other item's link names the blocker) and the state event whose `blocked_by` names it are now in `source_event_ids`. The 0.1.19 note that a blocker named only by another item's `blocks` link "is not yet named" no longer holds. A Blockers row with no source raises an internal error; the bounded card is unchanged. `item_spec_moved` also reads the item and its events in one snapshot, so a `spec moved` note never matches a state the store did not pass through.
- **`ledger --json card` carries `cuts`.** It is one `{section, count, unit, command}` entry per cut marker the text card prints, in the same order and from the same functions, so both forms use the same count and command; `truncated` is unchanged. Blockers past five that come from links are counted and pointed at the query, while blockers named only in the item's own `blocked_by` still point at `export-item`.
- **`ledger query --open`** keeps only the items that are not closed and combines with the other filters. The cut Blockers section now prints `query --blocks=ITEM --open --all`, so it returns the rows the card counted; `query --blocks` without `--open` returns what it did before.
- **`doc.attached` event and `ledger doc add`, `ledger doc show` and `ledger doc verify`.** `doc add ITEM FILE --role ROLE` attaches a document to an item as one `doc.attached` event with its bytes stored inline; `doc show` lists an item's documents (`--id N --content` prints one document's stored bytes after re-hashing them); `doc verify` re-hashes every stored document and exits 4 on a mismatch. `show` and `verify` read only the ledger. `doc add` refuses a FILE that is not a regular file or that is reached through a symlink component.
- **Credential-shape scan (`ledger/credscan.py`).** It looks for private-key headers and GitHub, AWS, OpenAI and Slack token shapes, and reports the shape name and byte offset, never the matched bytes. It runs at three points: `doc add` refuses a match as `doc_credential_shape` (`--allow-credential-shapes` attaches it and marks the event `credential_override: true`); restore and replay refuse an unmarked match as `restore_doc_credential`; and `export` scans pending documents before it writes any file and refuses an unmarked match as `export_doc_credential`, leaving the watermark where it was. A hit stops the export and any commit; the commit and push steps do not re-scan files already in the export directory. It is a tripwire for these common shapes, not a secret detector.
- **Other visible changes with `doc.attached`.** `ledger context` lists an item's attached documents in its Linked documents section; `doc add ITEM -` reads the document from standard input; and `doc.attached` is a reserved kind, so a generic append refuses it.
- **`export-item --ticket` fence and line numbers match the roadmap importer.** A fence may be indented by zero to three spaces only, so a tab or a non-breaking space no longer opens one (the search index applies the same fence rule, so a tab-indented fence no longer opens a fence there either); and lines are counted by newline alone, so a form feed, U+0085 or U+2028 above a ticket no longer makes the view print the neighbouring ticket.
- **`ledger events` text output escapes every line-boundary character in the payload column**, so one event stays on one line when a payload holds U+0085, U+2028 or U+2029. The payload column now escapes every non-printable character, as the other columns do; the JSON form is unchanged.
- **The `ledger doctor` search line names its cause and remedy in a `warn` state.** It reads `(warn: ledger index refuses: REASON; to clear it, REMEDY)` instead of claiming `ledger index` rebuilds the index, which it refuses to do there. An unknown reason prints verbatim with a pointer to `ledger doctor --json`; the `ok` and `info` lines and the JSON output are unchanged.
- **`docs/cli.md` states which legacy ticket ids `export-item --ticket` refuses.** It follows the importer's id grammar: a ticket id in lowercase or with an underscore carries no stable id, so no anchor to it exists and the verb refuses it `row_source_anchor_not_found` (exit 1); rename the id to fix it. No code changed.
- **README and `KNOWN-LIMITS.md` wording.** The README verb table now lists `contract`, `events`, `links` and `questions`, and a test fails when a registered verb has no row; the known-limits text no longer names an internal ticket id.

Upgrade with `@v0.1.20`.

## 0.1.19 — 2026-11-11

New read verbs (`links`, `contract`, `events --id`), an unbounded `card --full`, a cheaper search-index catch-up, and a `verify` that replays the child-of forest.

- **`ledger links ITEM`** prints every link that names the item, in both directions: its own outgoing links and other items' links that target it. Each row says how the other end resolves: `item` (with its title), `unresolved` (printed, never dropped) or `external`. An unknown item is refused `not_found`.
- **`ledger events --id N`** (repeatable) prints exactly those events in id order. A missing id is refused `not_found`, naming every missing id; combining `--id` with another filter is a usage error. The text form now escapes tabs and newlines in the kind, item and actor columns, so one event stays on one line.
- **`ledger contract`** prints the agent contract from the installed package, and never opens the database. The page now ships inside the wheel, and `resume --brief` points to the verb.
- **`ledger card --full`** prints the card unbounded: every row, word, finding, pointer and source id, with no cut marker. Its `source_event_ids` also name each decision record's standing, result and superseding events and the creation, state and `child-of` link events behind each child row (a blocker named only by another item's `blocks` link is not yet named), and the whole card is read from one database snapshot. The bounded card shows the newest 8 links with a count of the rest.
- **Every cut card section names a command that prints its rows.** `query` gains `--duplicate-of ID` and `--blocks ID` for inbound duplicate rows and for blockers named only by another item's link.
- **`ledger --json resume`** always carries `open_without_stop`, so a ledger whose open items have no stop no longer reads as empty.
- **`ledger verify`** replays every event into an empty in-memory copy of the schema and checks each `child-of` link with the write path's own guard. The first refusal fails with exit 4 and the guard's code (`second_anchor`, `self_link`, `anchor_missing`, `cycle`); an event the guard cannot evaluate is reported `unevaluable`, never skipped.
- **Search index:** a `behind` index catches up from the new events without re-reading markdown (the new `refresh` field says `append` or `rebuild`). `index_status` and doctor carry a `severity` (`ok`, `info`, `warn`). With no `search_roots`, `ledger index` says only the ledger is indexed and names the setting to add. A ref `some/dir/` is stored as `some/dir`. A non-regular file (such as a FIFO) at the index path is refused instead of blocking the search.
- **`export-item --ticket`** finds tagged (`### Ticket: [tag] ID — title`) and bare (`### ID — title` inside `## Tickets`) ticket headings, as the roadmap importer does.
- **The SQLite-not-found refusal's remedy is narrowed:** it no longer suggests a python.org build (CPython's macOS installer links SQLite statically), and the message no longer doubles its full stop before the path. A test now fails when the newest dated CHANGELOG heading does not name the `pyproject.toml` version.
- **`KNOWN-LIMITS.md`** is new: it lists the known limits of this alpha (a FIFO planted beside the search index hangs `ledger search`; control characters in an item id break the printed resume and card-footer commands; rows stored before 0.1.15, or restored from older exports, print an extra separator in `ledger questions`). The threat model is one trusted operator on their own machine: a finding is out of scope when its trigger needs a hostile local process or user, or input the operator's own use does not produce. The file's opening paragraph states the full rule.

Upgrade with `@v0.1.19`.

## 0.1.18 — 2026-11-10

The SQLite-not-found refusal now names its usual cause and the remedy.

- **When no library exposes SQLite's C API, the refusal names the usual cause and the remedy.** The usual cause is a Python build from python-build-standalone (which uv-managed Python uses), which links SQLite statically; the remedy is a Python whose `sqlite3` module links a shared `libsqlite3`, such as a distro or python.org build. CI also runs the suite on the runner's distro `python3`.

Upgrade with `@v0.1.18`.

## 0.1.17 — 2026-11-10

`ledger --version` reads the package's own version constant.

- **`ledger --version` prints `ledger.__version__`, a string constant in `ledger/__init__.py`.** The CLI no longer imports `importlib.metadata`, and no module under `ledger/` does. The constant is bumped with `pyproject.toml` at each release; a test fails when the two differ.

Upgrade with `@v0.1.17`.

## 0.1.16 — 2026-11-09

First-write and first-resume output now names the action a new user needs.

- A write without `LEDGER_HARNESS` suggests `LEDGER_HARNESS=claude-code`, and `item.stopped` phase errors list the allowed phases from the schema.
- `ledger resume` reports how many open items lack a stop instead of saying there are no open items. Runnable commands from `resume` and `card` preserve `LEDGER_PROG`, `python3 -m ledger`, or the executable basename, and include `--db` only when it was passed.
- Top-level `--version` prints `ledger <version>` without requiring a database.
- Abbreviated long options (for example `--d` for `--db`) are no longer accepted; spell the option in full. A usage error (exit 2) results.
- The install and upgrade docs now use `pipx`, the database home is spelled one way, and a start-here page, `docs/start-here.md`, takes a new person from nothing to a working ledger.
- **The agent contract now says how to file a follow-up mid-session.** Make a source from the parent's id, a colon and a short slug of the follow-up (`it-1a2b3c4d5e6f:flaky-retry`), create the follow-up with `ledger new` and that source, then link it to the parent with `ledger link NEW --rel child-of --target PARENT`. If you are interrupted between the two, run both again unchanged: one child and one link remain. The follow-up line in the `ledger resume` brief prints the same commands as `docs/agent-contract.md`.

Upgrade with `@v0.1.16`.

## 0.1.15 — 2026-11-09

An answer opens a gate only when the harness captured it for its ask.

- **`ledger.answers.bound_answer(lg, K)`** is the one reader a gate calls: it returns the first election linked to ask K when that election quotes the harness's picker capture of the owner's answer to K, and otherwise the reason it does not count (`pre_binding`, `typed_answer`, `answer_other_item`, `answer_before_ask`, `answer_unbound`, `answer_other_ask`, `capture_conflict`, `display_fingerprint_bad`, `ask_not_pickable`, `display_differs`, `picker_multi_question`, `picker_typed`, `picker_ambiguous`, `picker_unanswered`, `picker_annotated`, the quote rule's code, `choice_mismatch`, `election_differs`; checked in that order). Every election recorded before this release reads `pre_binding`; nothing is migrated.
- **`ledger capture --kind prompt|picker`** is new, for the harness's hooks only. It reads one hook JSON object on stdin and writes the prompt on channel `hook:prompt`, or the AskUserQuestion call on channel `hook:picker` with a `picker` object: the displayed questions verbatim, their fingerprint, the answers map, any annotations, the chosen option's position and the ask an `[ask K]` tag names. `utterance.captured` gains the optional `picker` field.
- **The `hook:` channel prefix is reserved.** `append`, `append-many`, the library's `append` and every other writer refuse any channel that starts with `hook:` (`reserved-capture-channel`), and a `picker` field off channel `hook:picker` is refused (`picker-needs-picker-channel`).
- **`ledger ask-display K`** is new: it prints the one-question AskUserQuestion input for ask K and its fingerprint. The question text names the ask's scope and, when it has one, its question id, so a grant id is on what the owner sees and in the fingerprint. `decision.asked` gains an optional `scope`; an ask without one, with fewer than two or more than four options, or with a repeated label is refused `ask_not_pickable`.
- **`ledger pick` now takes `ITEM --ask K` and nothing else.** It elects from the earliest picker capture for K, records the capture's fingerprint and option position (`decision.elected` gains the optional `display_sha256` and `chosen_position`), derives every other field from K, and checks the answer as a gate would before writing. The old `--question/--option/--chosen` form is refused with `pick_needs_ask`.
- **`ledger grants`**: a `granted` election is STANDING only when it answers an ask carrying its grant id and is that ask's bound answer; otherwise UNVERIFIED with the reason (`grant_unasked`, `not_first_answer`, or the gate's). A withdrawal still takes effect unverified. A grant recorded with `ledger elect` quoting a typed prompt no longer stands: raise the grant as an ask and record the picker answer with `pick --ask`.

An ask is refused when the text it displays holds a control character.

- **Control characters are refused in the text an ask displays.** The question id, the scope, the question, and each option's label and description are what the picker prints and what the display fingerprint and `ledger grants --check` match. A carriage return followed by an ANSI erase-line code could make a terminal show only the text after it while the stored value was longer, so the owner could approve a grant id they never saw. C0 (U+0000 to U+001F, so ESC and every ANSI escape), DEL (U+007F) and C1 (U+0080 to U+009F) are now refused, never escaped; a newline is allowed in the question only. Writing a `decision.asked`, or a `decision.elected` carrying the same text, with one is refused as `ask_control_character:<field>` (envelope code `validation`); a retry of a keyed ask stored before this release still returns the stored event. An ask already stored with one is not rewritten: `ledger ask-display` refuses it as `ask_not_pickable`, naming the field and the code point, so no pick, election or grant binds through it. `verify`, `export` and `restore` still read rows written before the rule.

The database descriptor check is bound to the connection's own descriptors.

- **The database descriptor check no longer refuses multi-threaded callers or a reopen.** 0.1.14 compared the whole process's open descriptors before and after each connect, so another thread's open in that window, or SQLite reusing a descriptor it already held, refused a valid connect (`db_fd_unknown`, and sometimes `db_not_regular`, `db_swapped` or `db_stat_failed`). The check now asks SQLite which descriptors the new connection holds, through SQLite's C library (`ctypes`), and compares each with the file at its name; it never reads the process's descriptor table. Concurrent connects on any threads, a process that opens a database it already has open, and other files opened meanwhile are accepted. 0.1.14 shipped with these refusals; upgrade to 0.1.15 rather than vendoring or pinning 0.1.14.
- **What is still refused.** The database descriptor must be the file at the path when it is checked and the file the path named before connecting; otherwise `db_swapped`. A path renamed away and back during the connect, or replaced by a database the process already holds open, now refuses `db_swapped` (0.1.14 said `db_fd_unknown`). The connection's own `-wal`, `-shm` and open journal descriptors must be the files at those names after the first read (`db_sibling_unsafe`). Every refusal from 0.1.14's name checks (symbolic link, FIFO, hard link, planted side files) is unchanged.
- **New refusal: `db_fd_unattributable`.** It means the ledger could not tell which descriptors the connection holds: the Python build hides SQLite's C functions, the connection capture could not be installed, a different SQLite library than the `sqlite3` module's was found, or a check on SQLite's internal file structures failed. The open is refused; there is no fallback to an unchecked open. `db_fd_listing_unavailable` is no longer raised. See "Opening the database file" in `docs/concepts.md`.
- **Accepted limit: a ledger open inside an exit handler.** A ledger open inside an exit handler that was registered before the process's first ledger open is refused (`db_fd_unknown`), because the capture hook is cancelled at exit. Follow-up: LEDGER-EXIT-HOOK1.

Upgrade with `@v0.1.15`.

## 0.1.14 — 2026-11-09

- **Database opens are bound to the files at their names.** The ledger now refuses a database path whose final component is a symbolic link, a non-regular file such as a FIFO, or a file with another hard link. It also checks that SQLite opened exactly that database and safely accounts for the `-wal`, `-shm` and `-journal` files before returning a connection. Point configurations that use a symbolic link at the database name at the real file instead. A process that holds a ledger connection open may be refused with `db_fd_unknown` when it reopens the same database: SQLite may reuse its descriptor. See "Opening the database file" in `docs/concepts.md` for this limit and the accepted first-creation window.

Upgrade with `@v0.1.14`.

## 0.1.13 — 2026-11-08

- **`export-item --ticket` reads an item whose source is a row key.** An item created with `--source row:<ID>` now prints its ticket from the locator in its newest `also at <locator>` note, where before it was refused with `invalid source locator`. The note text is exactly `also at ` and a locator in one of two forms: `path:line@commit`, or `path#ID`, the form the roadmap importer and `row-item.sh ensure` write today. A `path#ID` locator names no line or commit, so the section is the one headed `### Ticket: ID` in `path` at the current `HEAD` of the repository (a bare `### ID` heading and a table row are not found). A note of any other shape is skipped, and the last valid one of either form wins. An item with no valid `also at` note is refused with `row_source_no_also_at` (exit 1); a `path#ID` whose heading is absent is refused with `row_source_anchor_not_found`, and one that appears more than once with `row_source_anchor_ambiguous`. Nothing is guessed. An item whose source is a locator is read as before, and `new --source` still accepts any text.

Upgrade with `@v0.1.13`.

## 0.1.12 — 2026-11-07

- **A `child-of` link must name an item that exists.** The store now refuses a `child-of` event whose target is not an item, for `append`, `append-many` and `restore` as well as `link` (exit 3, code `anchor_missing`; a refused `append-many` batch writes nothing). Before, such a link was accepted and `parent_id` held the missing id for good. An anchor created earlier in the same batch or restore counts. An export that already holds a `child-of` link to a missing anchor, or a link that comes before its anchor's creation, now fails `restore` with `anchor_missing`.
- **`link --rel child-of` with an `--idempotency-key` is refused (code `key_on_noop`) when the child already has that anchor.** The call writes no event, so the key named nothing, and after the child later moved the same retry failed as `second_anchor`. Without a key the call still writes nothing and reports `unchanged`. A keyed call that wrote an event replays as before.
- **The Children section of an anchor's card keeps each child on one line.** A newline, carriage return, tab or other control character in a child's title or holder is shown as a visible escape (for example `\n`), so a title can no longer start a heading inside the card. `card --json` still carries the raw strings.

Upgrade with `@v0.1.12`.

## 0.1.11 — 2026-11-07

- **`ledger spec set` records a source-only change.** The same content with a different `--source` now writes the next spec version, carrying the same content and the new source, and the item's current spec source becomes the new one. The same content with the same source still writes nothing and reports `unchanged`, and so does a call with no `--source`. Moving the source back to an earlier value is another change. `--source-rev` still does not version the spec. Before, only a content change wrote a version, so a caller that moved the source alone could not record it. A claimant's card and `pick` print `spec moved: vN → vM` only when the content hash of the current spec differs from that of the version the claim stamped, so a source-only move does not report one; when the claimed version's event cannot be found, the version numbers decide as before.

Upgrade with `@v0.1.11`.

## 0.1.10 — 2026-11-07

- **`ledger query --json` rows carry `parent_id` and `children_open`,** the same two values `v_items` has: the item's current anchor (a string id, or `null`) and how many of its children are not closed (an integer, 0 or more). Only `query` gains them; the text output and every other command's JSON are unchanged.

Upgrade with `@v0.1.10`.

## 0.1.9 — 2026-11-07

Anchors and children. Nothing is renamed, and every command that worked in 0.1.8 works the same way. There is one schema migration (version 10) that replaces a view and leaves every event as it was.

- **`ledger link CHILD --rel child-of ANCHOR`** makes an item a child of an anchor. An anchor is an item that groups others; a child has one current anchor at a time. A second anchor is refused (exit 3, naming the current one) unless `--move` is passed, and a move appends a new link that names the anchor it left; earlier events never change. Linking again to the same anchor writes nothing. A link that would make an item its own ancestor, including a link to itself, is refused and writes nothing. `--move` is accepted only with `--rel child-of`. The store holds the same rules for every writer: a `child-of` event written with `append` or `append-many`, or replayed by `restore`, is refused the same way (a second anchor needs `moved_from` naming the current one), and a refused `append-many` batch writes nothing.
- **The anchor's card has a Children section** with one line per child (id, title, status, holder), a child that `blocks` another first and creation order otherwise, under a `closed/total` count. An item with no children shows no section. `card --json` carries the same rows and the count, and `resume` prints the count for an anchor. When a long card cuts the section, its marker names `ledger query --parent=ANCHOR --all`, which lists every child the anchor has when the command runs.
- **`ledger query --parent ID`** lists the children of `ID`. **`ledger query --all`** removes the row cap; it cannot be combined with `--limit`.
- **A retry of `link --rel child-of` under the same idempotency key from a different session or actor is now refused as `conflict` (exit 3),** as on every other relation. Before, it returned the stored event.
- **`v_items` gains two columns,** after `spec_version`: `parent_id` (the item's current anchor, or NULL) and `children_open` (how many of its children are not closed). The migration is `0010_child_of.sql`.

Upgrade with `@v0.1.9`.

## 0.1.8 — 2026-11-06

Three read-only commands for looking at a store. Nothing is renamed, there is no schema change, and every command that worked in 0.1.7 works the same way.

- **`ledger events`** lists events across all items, newest first. `--since` / `--until` use the same date grammar as `query`; `--kind` is repeatable; `--item`, `--actor` and `--limit` narrow the list. Filters combine with AND.
- **`doctor --json`** adds a `summary` object: counts of items by type and status, the event log's size and newest timestamp, open questions, claimed items, including expired ones (which `expired_claims` lists), and (when `LEDGER_EXPORT_DIR` is set) how far the export watermark is behind. A read failure on the summary or the watermark marks that part `unknown` and never changes doctor's exit code. There is no `last_export_at` because the watermark sidecar records only an event id, and no `verify` field because verify runs are not recorded anywhere.
- **`ledger questions`** lists open asks across all items, oldest first, with each item's current type, title and status.

Upgrade with `@v0.1.8`.

## 0.1.7 — 2026-11-05

Test-only release; the CLI is unchanged from 0.1.6. A new test pins the `rel` check a `duplicate-of` call makes on a lone event stored under its idempotency key: a plain link from the same item to the same target, under the same key, is refused as `conflict` and writes nothing (LC36).

Upgrade with `@v0.1.7`.

## 0.1.6 — 2026-11-05

`ledger link --rel duplicate-of --idempotency-key K` now reserves `K` itself. Before, it wrote its two events under `K:link` and `K:close` and never looked at `K`, so a plain `link` under `K` followed by a `duplicate-of` call under the same `K` succeeded, wrote both events and closed the item. Now the link event of a duplicate-of is written under `K` (the same slot a plain link uses) and the close under `K:close`. The same key with another mode, target or content is refused as `conflict` (exit 3) and writes nothing; a retry of the same call still returns both stored events; only one of the pair present still refuses as `partial_write`. A duplicate-of written under 0.1.5 stored its link as `K:link`, which this version does not read as `K`; retry such a call across the upgrade with a new key (LC34).

Upgrade with `@v0.1.6`.

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
