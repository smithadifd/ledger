# Vendoring

The `ledger/` directory of this repository is the package's single source. A
repository that uses the ledger carries a copy of it, byte for byte, at one
path, pinned to a tag or commit of this repository, and checks the copy for
drift. This file defines the mapping that check compares along.

## What is vendored

Every file tracked under `ledger/` in this repository, and nothing else:

| This repository | Consumer repository |
|-----------------|--------------------|
| `ledger/<path>` | `skills/_lib/ledger/<path>` |

The mapping is one to one: the same relative path, the same bytes. That
includes the package's own tests (`ledger/tests/`, with its fixtures) and
its migrations (`ledger/migrations/`). Nothing outside `ledger/` is vendored:
`README.md`, `docs/`, `VENDORING.md`, `LICENSE`, `pyproject.toml`, `tests/`,
`.github/` and `.gitignore` belong to this repository only. Untracked
interpreter caches (`__pycache__/`) are never part of the mapping.

All vendored files are regular files with mode `100644`; there are no
symlinks and no executables.

## The drift check

The consumer's check fails on any difference between its copy and the pinned
commit of this repository:

1. for every file tracked under `ledger/` at the pinned commit, the file at
   the mapped consumer path exists and its bytes are identical;
2. the consumer has no tracked file under `skills/_lib/ledger/` that the
   pinned commit lacks under `ledger/`.

One changed byte, a missing file or an extra file is a failure. The consumer
never edits its copy: a change lands here first, gets a tag, and the consumer
bumps its pin and re-copies the tree in one change.

## Carve record

This repository was carved, without history, from `skills/_lib/ledger/` of
the claude-toolkit repository at commit
`b96a61b74e15459c2988d4274aafa55b7593b499` (schema migrations 0001 to
0008).

- All 80 files of that directory were copied unchanged: `cmp` against the
  source commit's bytes shows no difference for any of them, so the mapping
  above holds in both directions for them.
- One file is new in this repository and has no counterpart at the carve
  commit: `ledger/__main__.py`, which makes `python3 -m ledger` run the CLI.
  A consumer gains it with the first vendored copy.
- The source repository's own command wrapper (`scripts/ledger` there) was
  not carried over: it wires that repository's plugin verbs and search root
  into `ledger.cli.main`, which is the embedding described in
  [docs/embedding.md](docs/embedding.md), and stays with that repository.
