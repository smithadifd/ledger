# Security policy

## Reporting a vulnerability

Report a vulnerability privately through the "Report a vulnerability" button on this repository's Security tab. Private vulnerability reporting is enabled, and the report reaches the maintainer without a public record. Do not open a public issue for a security problem.

Say which verb or module is affected, what you ran, and what happened. `ledger --json` output and the exit code help. You will get a reply in the private report.

## Scope

The ledger trusts whoever can write its database file. Its SQLite triggers guard against accidental edits, not against an attacker with write access, who can replace the file outright; `verify`, the export and its git history are what make tampering visible. A report that `verify` passes on a tampered ledger, that an export or restore accepts what it should refuse, or that a verb writes outside its documented path is in scope.

## Supported versions

Fixes go to the latest release. Update to the latest tag before reporting; a problem that is already fixed there is closed with a pointer to the fix.
