#!/bin/sh
# PostToolUse on AskUserQuestion: pass the hook input on stdin to
# `ledger capture --kind picker`, the capture `ledger pick ITEM --ask K` binds.
# The capture is a write, so it stamps the harness: claude-code when
# LEDGER_HARNESS is unset, empty or exactly `cli`, else the value as it is (see
# common.sh).
# Stdout stays empty. A failure is one stderr line with no answer text. Exits 0
# on every path. Off (no output, no write) unless a `ledger` command is found
# and LEDGER_DB is set (from the environment or a plugin option).
_hooks_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd) || exit 0
. "$_hooks_dir/common.sh" || exit 0
ledger_hooks_prepare || exit 0
[ -n "$LEDGER_HOOKS_CMD" ] || exit 0
[ -n "${LEDGER_DB:-}" ] || exit 0
LEDGER_HARNESS="$LEDGER_HOOKS_HARNESS" "$LEDGER_HOOKS_CMD" capture --kind picker >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] || printf 'ledger-hooks: picker capture failed (exit %s)\n' "$rc" >&2
exit 0
