# Releasing

A checklist of what to run for a ledger tag, built from the steps used for
v0.1.21 and v0.1.22. It is not a grant of who may tag, merge, or push.

Replace `X.Y.Z` with the version being tagged.

## 1. Open a `chore(release)` pull request

The last two tags used a squash-merged pull request whose tree changed
exactly these three files:

- `pyproject.toml` — set `[project] version` to `"X.Y.Z"`
- `ledger/__init__.py` — set `__version__` to `"X.Y.Z"`
- `CHANGELOG.md` — add the dated section at the top of the file

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

## 4. After the tag: rerun the mods ledger tests against the new tag

After the tag, rerun the mods ledger tests against the new tag. In the
`smithadifd/mods` repository, run `claude plugin test ledger`, and recapture
the `/ledger` mod's captured fixtures from the new tag if their shape moved.
