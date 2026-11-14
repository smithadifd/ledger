# Known limits

Findings that are real but out of scope for alpha. The alpha threat model is one trusted
operator on their own machine. A finding is out of scope when its trigger needs a hostile
local process or user, or input at a size or shape the operator's own use does not produce,
and it falls in none of the categories that stay in scope: secrets and credentials; data
loss or corruption in ordinary use; the merge gate's own integrity; and anything reachable
by content that arrives from outside the machine (pull request text and diffs, fetched
pages, tracker content). When a reviewer is unsure, the finding stays in scope.

A review writes an out-of-scope finding as a Minor note,
`known-limit: <path>:<line> — <what> — out of scope for alpha: <why>`, with no tag group and
no weight group. The seat copies it here as one line. It gets no ticket, no fix round and no
block. The line is revisited at beta, when a second person's machine and use are in the
picture.

Line form, one per limit. A limit a review recorded names the pull request and review round
that found it:

    - <path>:<line> — <what> — out of scope for alpha: <why> (<repo>#<PR> r<round>)

A limit set by a ruling names the pull request that recorded the ruling, who made it and the
date (`owner confirmation 2026-11-10`, `director ruling 2026-11-11`):

    - <path>:<line> — <what> — out of scope for alpha: <why> (<repo>#<PR>, <ruling> <date>)

## Limits

- ledger/search.py:655 — `_open_index` checks only the index path with `lstat`, so a FIFO planted at `search.db-journal`, `-wal` or `-shm` beside the index hangs `ledger search` and `ledger doctor` — out of scope for alpha: the trigger is someone creating a FIFO inside the ledger's own directory, which the operator's use never does, and the index is a derived cache (foundry#581, owner confirmation 2026-11-10)
- ledger/verbs/__init__.py:124 — `_shell_escape` writes a C1 control character such as U+0085 as `\xHH` inside `$'...'`, which the shell turns into a raw byte, so the printed resume command fails when run — out of scope for alpha: ids the importer and row-item mint use ASCII letters, digits and hyphens, so a C1 character in an id needs a hand-made id, and the command fails loudly (foundry#581, owner confirmation 2026-11-10)
- ledger/card.py:586 — `recover_command` and `footer_line` (`:591`) build the printed command with `shlex.join`, which leaves a newline or other control character in an item id in place, so the card footer and cut markers break across lines — out of scope for alpha: same trigger as the line above (an id written by hand rather than minted by the importer), with a cosmetic effect: the printed command spans lines but still names the item (foundry#581, owner confirmation 2026-11-10)
- ledger/verbs/questions.py:80 — `ledger questions` text output — rows stored before ledger#34, or restored from older exports, print an extra separator — out of scope for alpha: legacy rows the operator's current writes no longer produce (foundry#582, director ruling 2026-11-11)
- ledger/verbs/capture.py:3 — A setup with no guard rail cannot stop an agent from running `ledger capture` itself: the plugin's hooks record answers but do not prevent a forged capture, so without a rule that denies an agent `ledger capture`, a picker answer is only as trustworthy as the agent — out of scope for alpha: the threat model is one trusted operator, and the guard rail belongs to the harness that runs the agent, not to the ledger (ledger#71, owner checkpoint 2026-11-12)
- plugins/ledger-hooks/hooks/prompt.sh:10 — A prompt captured right after a model switch may carry the previous model, or none: the UserPromptSubmit hook's `ledger capture` can run while the PostModelSwitch hook is still recording the new model, and nothing orders the two — out of scope for alpha: the capture records the person's words, which are right; only its model stamp can lag one prompt, and ordering captures against a switch is its own follow-up (LEDGER-MODEL-SWITCH-STAMP1, owner election 2026-11-14)
- ledger/session_model.py:337 — Two model switches with no prompt between them (an automatic switch and one the person asks for) can be recorded in the wrong order: Claude Code's PostModelSwitch input carries no ordering field and its hooks reference does not say whether such hooks run in order, so the ledger orders them by when each hook process starts, and the later-starting one wins — out of scope for alpha: the next switch or session start corrects it, and every plugin session recorded no model at all before (LEDGER-MODEL-SWITCH-STAMP1, owner election 2026-11-14)
- ledger/session_model.py:61 — A PostModelSwitch hook that is still running five seconds after the next prompt is sent, or that refuses on its two-second budget, leaves the previous model recorded, and that request's writes and later ones stamp it until the next switch or session start. A refusal shows the person a hook-error notice; Claude is not told. A hook that starts late but finishes within its budget after the request was sent is correct for later requests but late for that one, and nobody is told — out of scope for alpha: Claude Code runs the event asynchronously and waits at most five seconds for it, which is the only ordering it offers (LEDGER-MODEL-SWITCH-STAMP1, owner election 2026-11-14)
- ledger/session_model.py:229 — `ledger resume --hook-model` refuses (`hook_model_no_folder`) while the database's folder does not exist, so the first session on a new ledger shows `ledger-hooks: model record failed (exit 3)` ahead of the brief and its writes stamp `LEDGER_MODEL` (or none) until the next switch or session start — out of scope for alpha: the folder exists from the first write on, and the hook never creates folders it was not given (LEDGER-MODEL-SWITCH-STAMP1)
- ledger/session_model.py:235 — One small model record and one lock file per session accumulate in `<database>.session-models` beside the database, and nothing prunes them — out of scope for alpha: each is under 4096 bytes, and pruning is a new delete path with its own review (LEDGER-MODEL-SWITCH-STAMP1)
- plugins/ledger-hooks/hooks/prompt.sh:10 — An invoked hook runs the first `ledger` on the `PATH` Claude Code gives it (`session-start.sh` and `picker.sh` do the same), so a different `ledger` earlier on that `PATH` runs instead and receives the session's prompts and picker answers; the plugin takes no absolute path and has no setting for one, so `PATH` order is the only control — out of scope for alpha: the threat model is one trusted operator, whose own `PATH` decides which `ledger` runs, as it does for every command the operator types (ledger#71, owner ruling 2026-11-13)
- ledger/dbfile.py:339 — A file moved onto a live `-wal` or `-shm` name by anything other than a ledger verb is still removed by a later last closer that is not the refused connection (an older ledger, the sqlite3 CLI). Tracked by LEDGER-SWAP-DISPLACED-WAL1 — out of scope for alpha: the trigger is another process moving a file onto a live side name of an open database, and the ledger's own `backup` and `restore` now refuse such destination names (ledger#88 r2)
