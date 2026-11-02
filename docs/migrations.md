# Migrations

The schema is a numbered series of SQL files in `ledger/migrations/`, named
`NNNN_name.sql`, shipped inside the package.

## Rules

- **Forward only.** A migration is never edited or removed once released; a
  change is a new, higher-numbered file.
- **Events are never rewritten.** After `0001`, migrations change views and
  indexes only. They do not add, change or delete a row in `events`, so the
  hash chain and every exported file are the same before and after.
- **Applied on open.** Opening a database for writing applies every packaged
  migration it has not recorded, in version order, and records each in the
  `schema_version` table. The first write to a new path creates the file and
  applies them all; `doctor` prints the highest version applied.
- **Newer databases are refused.** A database that records a version this
  copy of the ledger does not ship was written by a newer ledger. Every open
  path refuses it, read or write, with a message naming the unknown
  versions, rather than read a schema it does not know. Each write
  transaction checks again under its write lock, so a newer writer migrating
  the file mid-session is caught too. Update the ledger to open it.
- **Restore is version-independent.** An export holds events, not schema, so
  restoring it into a new database applies the current migrations.

## Adding a migration

1. Add `ledger/migrations/NNNN_short_name.sql` with the next number. Start
   with a comment that says what changes and why.
2. When a view changes, drop and recreate every view that reads it, in
   dependency order, unchanged apart from the change (see `0008` for the
   pattern).
3. Add tests in `ledger/tests/` that open an older database and check the
   new view, and update any test that pins the latest version number.

## History

| Version | File | Change |
|--------:|------|--------|
| 1 | `0001_initial.sql` | the `events` table, its indexes and append-only triggers, and the first views (`v_items`, `v_ready`, `v_blocked`, `v_open_decisions`) |
| 2 | `0002_open_decisions_ask_link.sql` | an election with `asked_event_id` closes exactly that question; question-text equality no longer closes one |
| 3 | `0003_loop_writer_identity.sql` | a loop-managed item can only be closed by the loop's own `loop.closed` event |
| 4 | `0004_open_decisions_held.sql` | `decision.held` withdraws one question without changing the item's state |
| 5 | `0005_decision_records.sql` | `v_decision_records`: each decision record with its current standing and result |
| 6 | `0006_claim_lease.sql` | `v_leases`; `v_ready` treats a claim whose lease expired as open |
| 7 | `0007_actor_operator.sql` | `v_events`, and an `operator` column on `v_leases`, split from the `<operator>/<agent>` actor |
| 8 | `0008_item_spec_version.sql` | `v_items.spec_version`, the item's current spec version |
