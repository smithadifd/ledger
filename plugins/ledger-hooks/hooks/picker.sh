#!/bin/sh
# PostToolUse on AskUserQuestion: pass the hook input on stdin to
# `ledger capture --kind picker`, the capture `ledger pick ITEM --ask K` binds.
# Stdout stays empty. A failure is one stderr line with no answer text. Exits 0
# on every path. Off (no output, no write) unless a `ledger` is on PATH and
# LEDGER_DB is set.
command -v ledger >/dev/null 2>&1 || exit 0
[ -n "${LEDGER_DB:-}" ] || exit 0
LEDGER_HARNESS="${LEDGER_HARNESS:-claude-code}" ledger capture --kind picker >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] || printf 'ledger-hooks: picker capture failed (exit %s)\n' "$rc" >&2
exit 0
