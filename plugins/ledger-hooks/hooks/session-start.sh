#!/bin/sh
# SessionStart: stamp the session's environment, record the session's start
# model, then print the ledger's brief. The ledger CLI does all the work; this
# shim exits 0 on every path, because its stdout is the brief. A failed model
# record is one line ahead of the brief, with no model text. The record needs
# LEDGER_HARNESS, as a write does; the hook supplies claude-code when it is
# unset. Off (no output, no write) unless a `ledger` is on PATH and LEDGER_DB
# is set.
command -v ledger >/dev/null 2>&1 || exit 0
[ -n "${LEDGER_DB:-}" ] || exit 0
input=$(cat)
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  printf '%s\n' "$input" | ledger resume --hook-env 2>/dev/null >>"$CLAUDE_ENV_FILE"
  rc=$?
  [ "$rc" -eq 0 ] || printf 'ledger-hooks: session env failed (exit %s)\n' "$rc" >&2
fi
printf '%s\n' "$input" | LEDGER_HARNESS="${LEDGER_HARNESS:-claude-code}" ledger resume --hook-model >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] || printf 'ledger-hooks: model record failed (exit %s)\n' "$rc"
brief=$(ledger resume --brief </dev/null 2>/dev/null)
rc=$?
if [ "$rc" -eq 0 ]; then
  printf '%s\n' "$brief"
else
  printf 'ledger-hooks: ledger resume --brief exited %s\n' "$rc"
fi
exit 0
