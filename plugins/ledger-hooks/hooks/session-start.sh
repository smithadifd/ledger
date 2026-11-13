#!/bin/sh
# SessionStart: stamp the session's environment, then print the ledger's brief.
# The ledger CLI does all the work; this shim exits 0 on every path. Off (no
# output, no write) unless a `ledger` is on PATH and LEDGER_DB is set.
command -v ledger >/dev/null 2>&1 || exit 0
[ -n "${LEDGER_DB:-}" ] || exit 0
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  ledger resume --hook-env 2>/dev/null >>"$CLAUDE_ENV_FILE"
  rc=$?
  [ "$rc" -eq 0 ] || printf 'ledger-hooks: session env failed (exit %s)\n' "$rc" >&2
fi
brief=$(ledger resume --brief </dev/null 2>/dev/null)
rc=$?
if [ "$rc" -eq 0 ]; then
  printf '%s\n' "$brief"
else
  printf 'ledger-hooks: ledger resume --brief exited %s\n' "$rc"
fi
exit 0
