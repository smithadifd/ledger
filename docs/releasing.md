# Releasing

A checklist of what to run for a ledger tag, built from the steps used for
v0.1.21 and v0.1.22. It is not a grant of who may tag, merge, or push.

Replace `X.Y.Z` with the version being tagged.

## Choosing the version

Patch releases carry fixes only. Any new verb, flag, hook or schema change
bumps the minor version. 0.1.13 and 0.1.20 carried features as patch
releases.

## 1. Open a `chore(release)` pull request

The release is one squash-merged pull request whose tree changes exactly these
paths:

- `pyproject.toml` — set `[project] version` to `"X.Y.Z"` (see Choosing the version)
- `ledger/__init__.py` — set `__version__` to `"X.Y.Z"`
- `plugins/ledger-hooks/.claude-plugin/plugin.json` — set `version` to
  `"X.Y.Z"`, the same value (between releases it reads the next patch with
  `-dev`, see section 4); the plugin list shows it, and
  `tests/test_plugin_hooks.py` fails when it is neither `__version__` nor that
  next `-dev` version
- `README.md` and `docs/start-here.md` — set the `#vX.Y.Z` pin in every
  `claude plugin marketplace add` command to `vX.Y.Z`, and remove the "from the
  next release (on main, not in vX.Y.Z)" markers from the sentences about
  features this release ships (the plugin's `bin/ledger`, the plugin options),
  so the pinned install is described as having them; find every marker with
  `grep -rniE "from the next release \(on main" README.md docs CHANGELOG.md`
- `plugins/ledger-hooks/ledger/` — refresh the bundled copy of `ledger/`
  (except `ledger/tests/`) so `tests/test_plugin_bin.py` still passes
- `CHANGELOG.md` — add the dated section at the top of the file
- `docs/upgrading.md` — name `X.Y.Z` as the current release, set the
  `#vX.Y.Z` pin in its `claude plugin marketplace add` command, and when the
  new changelog section has an "Upgrading" paragraph, carry what an upgrader
  needs from it into section 1.5

`tests/test_docs_pinned_install.py` fails when an install command in the
tracked Markdown files is unpinned, or pins a tag other than `v` followed by
`__version__`. An unpinned add follows main, so a person who ran it would get
plugin changes that no release has shipped.

The pull requests for v0.1.21 and v0.1.22 changed only `pyproject.toml`,
`ledger/__init__.py` and `CHANGELOG.md`.
`docs/upgrading.md` joined the list because it kept calling 0.1.22 the current
release through 0.2.0, 0.2.1 and 0.3.0.

In `docs/upgrading.md`, change every pinned tag (`@vX.Y.Z` and `#vX.Y.Z`), every printed
`ledger X.Y.Z` and every sentence that names the current release. Then list
every version number on the page and read each hit; each one is `X.Y.Z` or a
release named on purpose as history (for example, the 0.1.14 warning):

    grep -nE '[0-9]+\.[0-9]+\.[0-9]+' docs/upgrading.md

Shape of the changelog section (see the 0.1.22 and 0.1.21 entries):

    ## X.Y.Z — YYYY-MM-DD

    One-line summary of what the release changes.

    - **Visible change.** Detail.
    - **Another change.** Detail.

    Upgrade with `@vX.Y.Z`.

The section closes with that Upgrade line (the tag in backticks). v0.1.22's
squash commit message started `chore(release): v0.1.22 (#68)`.

## 2. Push-scan existing export repositories (when the export changed)

When the release changes the export credential scan or what export sends,
run the scan read-only against existing export repositories before tagging.
v0.1.22 waited on this step.

The exact invocation used for that wait is not recorded in this repository's
history; do not invent a flag. Run the same read-only scan the export path
uses, against the live export trees, and do not write to them.

Skip this step when the release does not change the export credential scan
or what export sends.

## 3. Annotated tag on the squash commit; tag only

After the pull request squash-merges, tag that squash commit (not a local
merge). v0.1.22's tag object points at commit `8e4fe35`. There is no GitHub
Release for v0.1.21 or v0.1.22.

Tag message shape (`git tag -n3 v0.1.22`):

    ledger v0.1.22: export --commit and export --push run the credential scan on what they send off the machine

Commands:

    git tag -a vX.Y.Z -m "ledger vX.Y.Z: <one-line summary>"
    git push origin vX.Y.Z

Push the tag only. Do not create a GitHub Release.

## 4. Right after the tag: bump the plugin manifest to the next `-dev` version

Main must not name a released version while its plugin tree differs from that
tag. Right after tagging, open a `chore` pull request that sets `version` in
`plugins/ledger-hooks/.claude-plugin/plugin.json` to the next patch with a
`-dev` suffix (`X.Y.(Z+1)-dev`). `tests/test_plugin_hooks.py` accepts exactly
that value or `__version__`. The next release's pull request sets it back to
that release's version (section 1).

## 5. After the tag: rerun the mods ledger tests against the new tag

After the tag, rerun the mods ledger tests against the new tag. In the
`smithadifd/mods` repository, run `claude plugin test ledger`, and recapture
the `/ledger` mod's captured fixtures from the new tag if their shape moved.
