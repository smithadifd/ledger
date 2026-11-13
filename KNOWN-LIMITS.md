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
- ledger/verbs/capture.py:3 — A setup with no guard rail cannot stop an agent from running `ledger capture` itself: the plugin's hooks record answers but do not prevent a forged capture, so without a rule that denies an agent `ledger capture`, a picker answer is only as trustworthy as the agent — out of scope for alpha: the threat model is one trusted operator, and the guard rail belongs to the harness that runs the agent, not to the ledger (owner checkpoint 2026-11-12)
