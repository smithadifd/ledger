#!/bin/sh
# PostModelSwitch: pass the hook input on stdin to `ledger resume --hook-model`,
# which records the session's new model beside the database, so later writes in
# this session stamp it. Stdout stays empty on success: Claude Code adds a
# PostModelSwitch hook's stdout to the model's context. A failure is one stderr
# line with no model text, and exit 2, so Claude Code shows the line to the
# user (exit 0 would hide it; the switch itself is never blocked). The record
# needs LEDGER_HARNESS, as a write does; the hook passes claude-code when it is
# unset, empty or exactly `cli`, and any other value as it is (see common.sh). Off (no output, no write) unless a `ledger` command is found and
# LEDGER_DB is set (from the environment or a plugin option).
_hooks_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd) || exit 0
. "$_hooks_dir/common.sh" || exit 0
ledger_hooks_prepare || exit 0
[ -n "$LEDGER_HOOKS_CMD" ] || exit 0
[ -n "${LEDGER_DB:-}" ] || exit 0
LEDGER_HARNESS="$LEDGER_HOOKS_HARNESS" "$LEDGER_HOOKS_CMD" resume --hook-model >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  printf 'ledger-hooks: model record failed (exit %s)\n' "$rc" >&2
  exit 2
fi
exit 0
