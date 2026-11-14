#!/bin/sh
# PostModelSwitch: pass the hook input on stdin to `ledger resume --hook-model`,
# which records the session's new model beside the database, so later writes in
# this session stamp it. Stdout stays empty on success: Claude Code adds a
# PostModelSwitch hook's stdout to the model's context. A failure is one stderr
# line with no model text, and exit 2, so Claude Code shows the line to the
# user (exit 0 would hide it; the switch itself is never blocked). Off (no
# output, no write) unless a `ledger` is on PATH and LEDGER_DB is set.
command -v ledger >/dev/null 2>&1 || exit 0
[ -n "${LEDGER_DB:-}" ] || exit 0
ledger resume --hook-model >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  printf 'ledger-hooks: model record failed (exit %s)\n' "$rc" >&2
  exit 2
fi
exit 0
