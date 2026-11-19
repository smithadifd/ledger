#!/bin/sh
# SessionStart: stamp the session's environment, record the session's start
# model, then print the ledger's brief. The ledger CLI does all the work; this
# shim exits 0 on every path, because its stdout is the brief. A failed model
# record is one line ahead of the brief, with no model text. The record needs
# LEDGER_HARNESS, as a write does; the hook passes claude-code when it is unset,
# empty or exactly `cli`, and any other value as it is (see common.sh). Off (no output, no write) unless a `ledger` command is found and
# LEDGER_DB is set (from the environment or a plugin option).
_hooks_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd) || exit 0
. "$_hooks_dir/common.sh" || exit 0
ledger_hooks_prepare || exit 0
[ -n "$LEDGER_HOOKS_CMD" ] || exit 0
[ -n "${LEDGER_DB:-}" ] || exit 0
input=$(cat)
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  if { ledger_hooks_export_options "$CLAUDE_ENV_FILE"; } 2>/dev/null; then
    { printf '%s\n' "$input" | "$LEDGER_HOOKS_CMD" resume --hook-env 2>/dev/null >>"$CLAUDE_ENV_FILE"; } 2>/dev/null
    rc=$?
    [ "$rc" -eq 0 ] || printf 'ledger-hooks: session env failed (exit %s)\n' "$rc" >&2
  else
    printf 'ledger-hooks: session env failed\n' >&2
  fi
fi
printf '%s\n' "$input" | LEDGER_HARNESS="$LEDGER_HOOKS_HARNESS" "$LEDGER_HOOKS_CMD" resume --hook-model >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] || printf 'ledger-hooks: model record failed (exit %s)\n' "$rc"
brief=$("$LEDGER_HOOKS_CMD" resume --brief </dev/null 2>/dev/null)
rc=$?
if [ "$rc" -eq 0 ]; then
  printf '%s\n' "$brief"
else
  printf 'ledger-hooks: ledger resume --brief exited %s\n' "$rc"
fi
exit 0
