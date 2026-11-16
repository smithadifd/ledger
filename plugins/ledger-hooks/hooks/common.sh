# Shared by the four ledger-hooks shims. Sourced, never executed.
# An already-set LEDGER_* value is kept; a plugin option fills only an unset
# or empty one. An empty option counts as unset.

ledger_hooks_quote() {
  printf "'"
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
  printf "'"
}

ledger_hooks_prepare() {
  LEDGER_DB_FROM_OPTION=
  LEDGER_OPERATOR_FROM_OPTION=
  LEDGER_HARNESS_FROM_OPTION=
  if [ -z "${LEDGER_DB:-}" ] && [ -n "${CLAUDE_PLUGIN_OPTION_LEDGER_DB:-}" ]; then
    LEDGER_DB=$CLAUDE_PLUGIN_OPTION_LEDGER_DB
    export LEDGER_DB
    LEDGER_DB_FROM_OPTION=1
  fi
  if [ -z "${LEDGER_OPERATOR:-}" ] && [ -n "${CLAUDE_PLUGIN_OPTION_LEDGER_OPERATOR:-}" ]; then
    LEDGER_OPERATOR=$CLAUDE_PLUGIN_OPTION_LEDGER_OPERATOR
    export LEDGER_OPERATOR
    LEDGER_OPERATOR_FROM_OPTION=1
  fi
  if [ -z "${LEDGER_HARNESS:-}" ] && [ -n "${CLAUDE_PLUGIN_OPTION_LEDGER_HARNESS:-}" ]; then
    LEDGER_HARNESS=$CLAUDE_PLUGIN_OPTION_LEDGER_HARNESS
    export LEDGER_HARNESS
    LEDGER_HARNESS_FROM_OPTION=1
  fi
  LEDGER_HOOKS_CMD=
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -x "${CLAUDE_PLUGIN_ROOT}/bin/ledger" ]; then
    LEDGER_HOOKS_CMD="${CLAUDE_PLUGIN_ROOT}/bin/ledger"
  else
    LEDGER_HOOKS_CMD=$(command -v ledger 2>/dev/null) || LEDGER_HOOKS_CMD=
  fi
  return 0
}

ledger_hooks_export_options() {
  envf=$1
  [ -n "$envf" ] || return 0
  if [ -n "$LEDGER_DB_FROM_OPTION" ]; then
    { printf 'export LEDGER_DB=%s\n' "$(ledger_hooks_quote "$LEDGER_DB")" >>"$envf"; } 2>/dev/null || return 1
  fi
  if [ -n "$LEDGER_OPERATOR_FROM_OPTION" ]; then
    { printf 'export LEDGER_OPERATOR=%s\n' "$(ledger_hooks_quote "$LEDGER_OPERATOR")" >>"$envf"; } 2>/dev/null || return 1
  fi
  if [ -n "$LEDGER_HARNESS_FROM_OPTION" ]; then
    { printf 'export LEDGER_HARNESS=%s\n' "$(ledger_hooks_quote "$LEDGER_HARNESS")" >>"$envf"; } 2>/dev/null || return 1
  fi
  return 0
}
