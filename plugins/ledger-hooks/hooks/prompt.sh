#!/bin/sh
# UserPromptSubmit: pass the hook input on stdin to `ledger capture --kind prompt`.
# The CLI decides what is stored (it skips harness frames and honours
# capture_prompts). Stdout stays empty: Claude Code adds it to the model's
# context. A failure is one stderr line with no prompt text. Exits 0 on every
# path. Off (no output, no write) unless a `ledger` is on PATH and LEDGER_DB
# is set.
command -v ledger >/dev/null 2>&1 || exit 0
[ -n "${LEDGER_DB:-}" ] || exit 0
LEDGER_HARNESS="${LEDGER_HARNESS:-claude-code}" ledger capture --kind prompt >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] || printf 'ledger-hooks: prompt capture failed (exit %s)\n' "$rc" >&2
exit 0
