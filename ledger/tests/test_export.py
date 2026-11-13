"""The credential scan at ``ledger export --commit`` and ``ledger export --push``.

``--commit`` stages into a private temporary index, scans the exact tree it
will commit and the new commit object, and moves only the pinned branch by a
compare-and-swap. ``--push`` scans every commit, path and blob reachable from
the pinned branch commit and pushes exactly that commit, by its full sha, to
the branch's push remote.

Tests are named for the design's T1 to T43 (T40 lives in the repository's
docs test); T44 to T46 cover review findings on the push certificate,
deeply nested event files and a branch name that is not UTF-8.
Credential samples are assembled at run time
(``test_credscan.SAMPLES``), so no complete shape appears in this file.
Remotes are local bare repositories. Every git command here, the test's own
and the code's, reads only this test's global config file and no system
config (``GIT_CONFIG_GLOBAL``, ``GIT_CONFIG_NOSYSTEM``). No test asserts the
outcome of a race: each injection runs at one fixed point, before the step
that reads it.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ledger import export as export_mod
from ledger.export import (
    ExportBranchCredential,
    ExportCommitAncestry,
    ExportCommitSignFailed,
    ExportDocCredential,
    ExportEventInvalid,
    ExportFileCredential,
    ExportGitFailed,
    ExportHeadDetached,
    ExportHeadMoved,
    ExportHistoryIncomplete,
    ExportObjectUnreadable,
    ExportPushFailed,
    ExportPushMultipleUrls,
    ExportPushUnborn,
    ExportPushUnconfigured,
    ExportRepoShallow,
    commit_export,
    event_filename,
    event_record,
    export_events,
    hash_body,
    push_export,
)
from ledger.store import Event, canonical_dumps, event_digest, open_ledger

from test_cli import invoke, seed
from test_credscan import SAMPLES, wrap

ITEM = "it-1"
SAMPLE = SAMPLES["openai-key"]
SAMPLE_TEXT = SAMPLE.decode("ascii")
HAND_ID = 9_000_000_000
EVENT_NAME = event_filename(HAND_ID)


# ---- isolation and helpers -------------------------------------------------------


def isolate_git_config(tmp_path: Path, monkeypatch) -> Path:
    """Point every git command at this test's global config and no system
    config, and drop variables that would point git at another repository."""
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("[commit]\n\tgpgSign = false\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_GRAFT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    return global_config


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch) -> Path:
    """Every git command reads this file as its global config and no system
    config, and no variable points git at another repository."""
    return isolate_git_config(tmp_path, monkeypatch)


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=check, env=env
    )


def out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.decode("utf-8").strip()


def init_repo(path: Path, branch: str = "main") -> Path:
    subprocess.run(
        ["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True
    )
    git(path, "config", "user.name", "ledger-test")
    git(path, "config", "user.email", "ledger-test@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    return path


def bare(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True, capture_output=True)
    return path


def remote_refs(remote: Path) -> dict[str, str]:
    lines = out(remote, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    return dict(line.split(" ", 1) for line in lines)


def has_object(repo: Path, oid: str) -> bool:
    return git(repo, "cat-file", "-e", oid, check=False).returncode == 0


def ref(repo: Path, name: str = "refs/heads/main") -> str | None:
    result = git(repo, "rev-parse", "--verify", "--quiet", name, check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def status(repo: Path) -> str:
    # GIT_OPTIONAL_LOCKS=0: status never rewrites the index it reads.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return git(repo, "status", "--porcelain", env=env).stdout.decode()


def snapshot(repo: Path) -> dict:
    """Every ref, HEAD, the real index bytes, the status and the git
    directory's top-level entries."""
    head = git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout
    index = repo / ".git" / "index"
    return {
        "refs": out(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": head or out(repo, "rev-parse", "HEAD"),
        "status": status(repo),
        "index": index.read_bytes() if index.exists() else None,
        "gitdir": sorted(os.listdir(repo / ".git")),
    }


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    seed(monkeypatch, path, ITEM)
    return path


def export(db: str, repo: Path) -> None:
    with open_ledger(db, create=False) as lg:
        export_events(lg, repo)


def exported_repo(tmp_path: Path, db: str, *, branch: str = "main") -> Path:
    """An export repository holding a fresh export and no commit."""
    repo = init_repo(tmp_path / "export", branch)
    export(db, repo)
    return repo


def committed_repo(tmp_path: Path, db: str) -> Path:
    """An export repository with one export committed by ``commit_export``."""
    repo = exported_repo(tmp_path, db)
    assert commit_export(repo).sha is not None
    return repo


def hand_commit(repo: Path, message: str, files: dict[str, bytes | None]) -> str:
    """A commit made by hand, with git itself: nothing scans it."""
    for rel, data in files.items():
        path = repo / rel
        if data is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return out(repo, "rev-parse", "HEAD")


def with_remote(repo: Path, remote: Path, name: str = "origin", branch: str = "main") -> Path:
    git(repo, "remote", "add", name, str(remote))
    git(repo, "config", f"branch.{branch}.remote", name)
    return remote


def doc_event(event_id: int, body: bytes, *, override: bool = False) -> Event:
    """A ``doc.attached`` record built by hand with its hash computed, so a
    test changes exactly one thing."""
    payload = {
        "role": "brief",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "mime": "text/plain",
        "content_b64": base64.b64encode(body).decode("ascii"),
    }
    if override:
        payload["credential_override"] = True
    event = Event(
        id=event_id, kind="doc.attached", payload_version=1, payload=payload,
        item_id=ITEM, idempotency_key=f"hand-{event_id}", actor=None, harness="pytest",
        model=None, session=None, channel=None, provenance_class=None,
        recorded_at="2026-01-01T00:00:00.000Z", prev_hash="0" * 64, hash="",
    )
    return rehash(event)


def rehash(event: Event) -> Event:
    return dataclasses.replace(event, hash=event_digest(hash_body(event)))


def record_bytes(event: Event) -> bytes:
    return (canonical_dumps(event_record(event)) + "\n").encode("utf-8")


def wrap_git(monkeypatch, hook) -> None:
    """Wrap the one git runner in ``ledger.export``: ``hook(args, run)``
    where ``run()`` runs the real command."""
    real = export_mod._git

    def wrapper(export_dir, *args, **kwargs):
        return hook(list(args), lambda: real(export_dir, *args, **kwargs))

    monkeypatch.setattr(export_mod, "_git", wrapper)


def cli(monkeypatch, *argv: str):
    """The CLI in text mode and in JSON mode: ``(code, out, err)`` for each."""
    return invoke(monkeypatch, list(argv)), invoke(monkeypatch, ["--json", *argv])


def executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def assert_no_sample(*texts: str) -> None:
    for text in texts:
        assert SAMPLE_TEXT not in text, "the output holds the credential sample"


# ---- commit ------------------------------------------------------------------------


def test_t1_a_second_commit_has_the_first_as_its_only_parent(db, tmp_path, monkeypatch):
    repo = exported_repo(tmp_path, db)
    first = commit_export(repo)
    assert first.sha is not None and first.index_refreshed is True
    seed(monkeypatch, db, "it-2")
    export(db, repo)
    # The tree the step must commit: the work tree staged onto the first
    # commit in a scratch index.
    scratch = {**os.environ, "GIT_INDEX_FILE": str(tmp_path / "expected.index")}
    git(repo, "read-tree", first.sha, env=scratch)
    git(repo, "add", "-A", env=scratch)
    expected_tree = git(repo, "write-tree", env=scratch).stdout.decode().strip()
    second = commit_export(repo)
    assert second.sha is not None
    assert out(repo, "rev-parse", f"{second.sha}^@") == first.sha
    assert out(repo, "rev-parse", f"{second.sha}^{{tree}}") == expected_tree
    assert ref(repo) == second.sha
    assert out(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert status(repo) == ""
    assert second.index_refreshed is True


def test_t2_an_unborn_branch_gets_a_parentless_commit(db, tmp_path):
    repo = exported_repo(tmp_path, db)
    assert ref(repo) is None
    result = commit_export(repo)
    assert result.sha is not None
    assert out(repo, "rev-list", "--parents", "-n", "1", result.sha) == result.sha
    assert ref(repo) == result.sha
    assert out(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(repo, "diff-index", "--cached", "--quiet", "HEAD", check=False).returncode == 0
    assert out(repo, "ls-files")
    assert result.index_refreshed is True


def test_t3_an_unborn_branch_with_nothing_to_commit_stays_unborn(tmp_path):
    repo = init_repo(tmp_path / "export")
    result = commit_export(repo)
    assert result.sha is None
    assert ref(repo) is None
    assert not (repo / ".git" / "index").exists()


def test_t4_a_hand_placed_unmarked_document_event_refuses_and_changes_nothing(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    (repo / EVENT_NAME).write_bytes(record_bytes(doc_event(HAND_ID, wrap(SAMPLE))))
    before = snapshot(repo)
    with pytest.raises(ExportDocCredential) as caught:
        commit_export(repo)
    message = str(caught.value)
    assert caught.value.code == "export_doc_credential"
    assert "export_doc_credential" in message
    assert EVENT_NAME in message and f"event {HAND_ID}" in message
    assert "openai-key" in message
    assert_no_sample(message)
    assert snapshot(repo) == before


def test_t5_a_hand_placed_file_holding_a_shape_refuses_and_changes_nothing(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    (repo / "notes.txt").write_bytes(wrap(SAMPLE))
    before = snapshot(repo)
    with pytest.raises(ExportFileCredential) as caught:
        commit_export(repo)
    message = str(caught.value)
    assert "export_file_credential" in message and "notes.txt" in message
    assert "openai-key" in message
    assert_no_sample(message)
    assert snapshot(repo) == before


def _record_dict(event: Event) -> dict:
    return event_record(event)


def _t6_bytes(case: str) -> tuple[str, bytes | None, str]:
    """``(file name, bytes, failing check)``; bytes None means a symlink."""
    event = doc_event(HAND_ID, b"plain notes\n")
    record = _record_dict(event)
    if case == "a-not-json":
        return EVENT_NAME, b"not json\n", "json"
    if case == "b-array":
        return EVENT_NAME, b"[1, 2]\n", "object"
    if case == "c-missing-field":
        del record["actor"]
        return EVENT_NAME, (canonical_dumps(record) + "\n").encode(), "missing field actor"
    if case == "d-id-not-name":
        return event_filename(HAND_ID + 1), record_bytes(event), "id"
    if case == "e-extra-field":
        record["zz_extra"] = base64.b64encode(wrap(SAMPLE)).decode("ascii")
        return EVENT_NAME, (canonical_dumps(record) + "\n").encode(), "canonical bytes"
    if case == "f-reordered":
        reordered = dict(reversed(list(record.items())))
        text = json.dumps(reordered, separators=(",", ":"), ensure_ascii=False)
        assert text != canonical_dumps(record)
        return EVENT_NAME, (text + "\n").encode(), "canonical bytes"
    if case == "g-hash":
        bad = dataclasses.replace(event, hash="0" * 64)
        return EVENT_NAME, record_bytes(bad), "hash"
    if case == "h-payload-key":
        bad = rehash(dataclasses.replace(event, payload={**event.payload, "zz": "x"}))
        return EVENT_NAME, record_bytes(bad), "payload"
    if case == "i-symlink":
        return EVENT_NAME, None, "json"
    if case == "j-not-utf8":
        return EVENT_NAME, b'{"id": "\xff"}\n', "utf-8"
    if case == "k-item-id":
        # A doc.attached event needs an item; the record is otherwise valid.
        bad = rehash(dataclasses.replace(event, item_id=None))
        return EVENT_NAME, record_bytes(bad), "item_id"
    raise AssertionError(case)


T6_CASES = [
    "a-not-json", "b-array", "c-missing-field", "d-id-not-name", "e-extra-field",
    "f-reordered", "g-hash", "h-payload-key", "i-symlink", "j-not-utf8", "k-item-id",
]


@pytest.mark.parametrize("case", T6_CASES)
def test_t6_an_event_named_file_that_is_not_a_valid_record_refuses(db, tmp_path, case):
    repo = committed_repo(tmp_path, db)
    name, data, check = _t6_bytes(case)
    if data is None:
        os.symlink("elsewhere.txt", repo / name)
    else:
        (repo / name).write_bytes(data)
    before = snapshot(repo)
    with pytest.raises(ExportEventInvalid) as caught:
        commit_export(repo)
    message = str(caught.value)
    assert "export_event_invalid" in message and name in message
    assert "(failed check: " in message and f"{check})" in message
    assert_no_sample(message)
    assert snapshot(repo) == before


def test_t7a_a_clean_filter_that_adds_a_shape_is_caught_in_the_staged_blob(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    leak = executable(tmp_path / "leak-filter.sh", f"#!/bin/sh\nexec sed 's/PLACEHOLDER/{SAMPLE_TEXT}/'\n")
    git(repo, "config", "filter.leak.clean", str(leak))
    (repo / ".gitattributes").write_text("notes.txt filter=leak\n", encoding="utf-8")
    (repo / "notes.txt").write_text("see PLACEHOLDER here\n", encoding="utf-8")
    assert SAMPLE not in (repo / "notes.txt").read_bytes()
    with pytest.raises(ExportFileCredential) as caught:
        commit_export(repo)
    assert "notes.txt" in str(caught.value)
    assert_no_sample(str(caught.value))


def test_t7b_a_clean_filter_that_changes_an_event_file_is_caught(db, tmp_path):
    repo = exported_repo(tmp_path, db)
    pad = executable(tmp_path / "pad-filter.sh", "#!/bin/sh\ncat\nprintf ' '\n")
    git(repo, "config", "filter.pad.clean", str(pad))
    (repo / ".gitattributes").write_text("[0-9]*.json filter=pad\n", encoding="utf-8")
    with pytest.raises(ExportEventInvalid) as caught:
        commit_export(repo)
    assert "(failed check: canonical bytes)" in str(caught.value)
    assert ref(repo) is None


def _attach_marked(monkeypatch, db: str, tmp_path: Path) -> None:
    source = tmp_path / "key.md"
    source.write_bytes(wrap(SAMPLE))
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "doc", "add", ITEM, str(source), "--role", "brief",
         "--allow-credential-shapes"],
    )
    assert code == 0, err


def test_t8_an_override_marked_document_exports_and_commits(db, tmp_path, monkeypatch):
    _attach_marked(monkeypatch, db, tmp_path)
    repo = init_repo(tmp_path / "export")
    code, body, err = invoke(
        monkeypatch, ["--json", "--db", db, "export", "--dir", str(repo), "--commit"]
    )
    assert code == 0, body + err
    data = json.loads(body)["data"]
    assert data["commit"] == ref(repo)
    assert data["index_refreshed"] is True


def test_t9_a_branch_moved_after_the_commit_object_refuses(db, tmp_path, monkeypatch):
    repo = committed_repo(tmp_path, db)
    seed(monkeypatch, db, "it-2")
    export(db, repo)
    old = ref(repo)
    index_before = (repo / ".git" / "index").read_bytes()
    moved = {}

    def hook(args, run):
        result = run()
        if args[0] == "commit-tree":
            hand = out(repo, "commit-tree", f"{old}^{{tree}}", "-p", old, "-m", "hand")
            git(repo, "update-ref", "refs/heads/main", hand, old)
            moved["hand"] = hand
        return result

    wrap_git(monkeypatch, hook)
    with pytest.raises(ExportHeadMoved) as caught:
        commit_export(repo)
    assert "export_head_moved" in str(caught.value)
    assert ref(repo) == moved["hand"]
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_t10_a_commit_object_without_the_old_head_as_parent_refuses(db, tmp_path, monkeypatch):
    repo = committed_repo(tmp_path, db)
    seed(monkeypatch, db, "it-2")
    export(db, repo)
    old = ref(repo)
    real = export_mod._git

    def wrapper(export_dir, *args, **kwargs):
        if args and args[0] == "commit-tree" and "-p" in args:
            at = args.index("-p")
            args = args[:at] + args[at + 2:]
        return real(export_dir, *args, **kwargs)

    monkeypatch.setattr(export_mod, "_git", wrapper)
    with pytest.raises(ExportCommitAncestry) as caught:
        commit_export(repo)
    assert "export_commit_ancestry" in str(caught.value)
    assert ref(repo) == old


def test_t11_a_detached_head_refuses_the_commit(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    git(repo, "update-ref", "--no-deref", "HEAD", ref(repo))
    (repo / "extra.txt").write_text("more\n", encoding="utf-8")
    before = snapshot(repo)
    with pytest.raises(ExportHeadDetached) as caught:
        commit_export(repo)
    assert "export_head_detached" in str(caught.value)
    assert snapshot(repo) == before


def test_t12_a_nested_repository_staged_as_a_gitlink_refuses(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    nested = init_repo(repo / "nested")
    hand_commit(nested, "nested", {"inner.txt": b"inner\n"})
    before = snapshot(repo)
    with pytest.raises(ExportObjectUnreadable) as caught:
        commit_export(repo)
    assert "export_object_unreadable" in str(caught.value)
    assert "gitlink" in str(caught.value)
    assert snapshot(repo) == before


def test_t13_a_file_name_holding_a_shape_refuses_without_printing_it(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    (repo / f"{SAMPLE_TEXT}.txt").write_text("plain\n", encoding="utf-8")
    before = snapshot(repo)
    with pytest.raises(ExportFileCredential) as caught:
        commit_export(repo)
    assert "a path name holds a credential shape (openai-key)" in str(caught.value)
    assert_no_sample(str(caught.value))
    assert snapshot(repo) == before


# ---- push --------------------------------------------------------------------------


def test_t14_a_credential_file_removed_later_refuses_the_push(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    old = hand_commit(repo, "add notes", {"notes.txt": wrap(SAMPLE)})
    hand_commit(repo, "remove notes", {"notes.txt": None})
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportFileCredential) as caught:
        push_export(repo)
    message = str(caught.value)
    assert f"commit {old}: notes.txt holds a credential shape (openai-key)" in message
    assert_no_sample(message)
    assert remote_refs(remote) == {}


def test_t15_an_unmarked_document_event_removed_later_refuses_the_push(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    old = hand_commit(
        repo, "add event", {EVENT_NAME: record_bytes(doc_event(HAND_ID, wrap(SAMPLE)))}
    )
    hand_commit(repo, "remove event", {EVENT_NAME: None})
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportDocCredential) as caught:
        push_export(repo)
    message = str(caught.value)
    assert f"commit {old}: {EVENT_NAME}: event {HAND_ID}" in message
    assert_no_sample(message)
    assert remote_refs(remote) == {}


def test_t16_an_override_marked_history_pushes(db, tmp_path, monkeypatch):
    _attach_marked(monkeypatch, db, tmp_path)
    repo = init_repo(tmp_path / "export")
    code, _out, err = invoke(monkeypatch, ["--db", db, "export", "--dir", str(repo), "--commit"])
    assert code == 0, err
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    pushed = push_export(repo)
    assert pushed.sha == ref(repo)
    assert remote_refs(remote) == {"refs/heads/main": pushed.sha}


def test_t17_a_detached_head_refuses_the_push(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    git(repo, "update-ref", "--no-deref", "HEAD", ref(repo))
    with pytest.raises(ExportHeadDetached):
        push_export(repo)
    assert remote_refs(remote) == {}


def test_t18_an_unborn_branch_refuses_the_push(db, tmp_path):
    repo = exported_repo(tmp_path, db)
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportPushUnborn) as caught:
        push_export(repo)
    assert "export_push_unborn" in str(caught.value)
    assert remote_refs(remote) == {}


@pytest.mark.parametrize("case", ["a-push-remote", "b-push-default", "c-branch-remote"])
def test_t19_the_push_goes_to_the_push_url_of_the_resolved_remote(db, tmp_path, case):
    repo = committed_repo(tmp_path, db)
    urls = {name: bare(tmp_path / f"{name}.git") for name in "ABCD"}
    git(repo, "remote", "add", "origin", str(urls["A"]))
    git(repo, "config", "remote.origin.pushurl", str(urls["B"]))
    git(repo, "remote", "add", "backup", str(urls["C"]))
    git(repo, "config", "remote.backup.pushurl", str(urls["D"]))
    git(repo, "config", "branch.main.remote", "origin")
    if case == "a-push-remote":
        git(repo, "config", "branch.main.pushRemote", "backup")
        git(repo, "config", "remote.pushDefault", "origin")
        target, remote = "D", "backup"
    elif case == "b-push-default":
        git(repo, "config", "remote.pushDefault", "origin")
        target, remote = "B", "origin"
    else:
        target, remote = "B", "origin"
    pushed = push_export(repo)
    assert (pushed.sha, pushed.branch, pushed.remote) == (ref(repo), "main", remote)
    for name, path in urls.items():
        expected = {"refs/heads/main": pushed.sha} if name == target else {}
        assert remote_refs(path) == expected, name


def test_t20_no_push_remote_configured_refuses_and_origin_stays_empty(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    origin = bare(tmp_path / "origin.git")
    git(repo, "remote", "add", "origin", str(origin))
    with pytest.raises(ExportPushUnconfigured) as caught:
        push_export(repo)
    assert "export_push_unconfigured" in str(caught.value)
    assert remote_refs(origin) == {}


def test_t21_tags_are_not_pushed_even_with_follow_tags(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    git(repo, "tag", "-a", "v1", "-m", "a tag")
    git(repo, "config", "push.followTags", "true")
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    pushed = push_export(repo)
    assert remote_refs(remote) == {"refs/heads/main": pushed.sha}
    kinds = out(remote, "cat-file", "--batch-all-objects", "--batch-check=%(objecttype)")
    assert "tag" not in kinds.split()


def test_t22_only_the_refspec_is_pushed(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    git(repo, "branch", "second")
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    git(repo, "config", "remote.origin.push", "refs/heads/main:refs/heads/other")
    git(repo, "config", "push.default", "matching")
    pushed = push_export(repo)
    assert remote_refs(remote) == {"refs/heads/main": pushed.sha}


def test_t23_configured_push_options_are_not_sent(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    git(remote, "config", "receive.advertisePushOptions", "true")
    record = tmp_path / "push-options.txt"
    executable(
        remote / "hooks" / "post-receive",
        f"#!/bin/sh\necho \"${{GIT_PUSH_OPTION_COUNT:-0}}\" > '{record}'\n",
    )
    git(repo, "config", "push.pushOption", "probe")
    push_export(repo)
    assert record.read_text(encoding="utf-8").strip() == "0"


def test_t24_a_credential_on_a_merged_side_branch_refuses(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    git(repo, "checkout", "-q", "-b", "side")
    side = hand_commit(repo, "side add", {"side.txt": wrap(SAMPLE)})
    hand_commit(repo, "side remove", {"side.txt": None})
    git(repo, "checkout", "-q", "main")
    hand_commit(repo, "main change", {"main.txt": b"main\n"})
    git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportFileCredential) as caught:
        push_export(repo)
    assert f"commit {side}: side.txt" in str(caught.value)
    assert remote_refs(remote) == {}


def test_t25_a_remote_tracking_ref_subtracts_nothing(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    old = hand_commit(repo, "add notes", {"notes.txt": wrap(SAMPLE)})
    hand_commit(repo, "remove notes", {"notes.txt": None})
    git(repo, "remote", "add", "origin", str(bare(tmp_path / "origin.git")))
    git(repo, "update-ref", "refs/remotes/origin/main", old)
    backup = bare(tmp_path / "backup.git")
    git(repo, "remote", "add", "backup", str(backup))
    git(repo, "config", "branch.main.pushRemote", "backup")
    with pytest.raises(ExportFileCredential) as caught:
        push_export(repo)
    assert f"commit {old}: notes.txt" in str(caught.value)
    assert remote_refs(backup) == {}


def test_t26_a_replace_ref_does_not_hide_history_from_the_scan(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    leaked = hand_commit(repo, "add notes", {"notes.txt": wrap(SAMPLE)})
    hand_commit(repo, "remove notes", {"notes.txt": None})
    parent = out(repo, "rev-parse", f"{leaked}^")
    clean = out(repo, "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", "add notes")
    git(repo, "replace", leaked, clean)
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportFileCredential) as caught:
        push_export(repo)
    assert f"commit {leaked}: notes.txt" in str(caught.value)
    assert remote_refs(remote) == {}


def test_t27_the_push_sends_the_pinned_commit_not_a_later_one(db, tmp_path, monkeypatch):
    repo = committed_repo(tmp_path, db)
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    pinned = ref(repo)
    late = {}

    def hook(args, run):
        if "push" in args:
            late["sha"] = hand_commit(repo, "late", {"late.txt": wrap(SAMPLE)})
            late["blob"] = out(repo, "rev-parse", "HEAD:late.txt")
        return run()

    wrap_git(monkeypatch, hook)
    pushed = push_export(repo)
    assert pushed.sha == pinned
    assert ref(repo) == late["sha"] != pinned
    assert remote_refs(remote) == {"refs/heads/main": pinned}
    assert not has_object(remote, late["blob"])


@pytest.mark.parametrize("where", ["message", "file-name"])
def test_t28_a_shape_in_a_commit_message_or_a_file_name_refuses(db, tmp_path, where):
    repo = committed_repo(tmp_path, db)
    if where == "message":
        sha = hand_commit(repo, f"note {SAMPLE_TEXT}", {"x.txt": b"x\n"})
        expected = f"commit {sha}: its commit object (headers and message) holds a credential shape"
    else:
        sha = hand_commit(repo, "add file", {f"{SAMPLE_TEXT}.txt": b"x\n"})
        expected = f"commit {sha}: a path name holds a credential shape (openai-key)"
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportFileCredential) as caught:
        push_export(repo)
    assert expected in str(caught.value)
    assert_no_sample(str(caught.value))
    assert remote_refs(remote) == {}


def test_t29_a_missing_object_refuses(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    hand_commit(repo, "add unique", {"unique.txt": b"only here\n"})
    blob = out(repo, "rev-parse", "HEAD:unique.txt")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportObjectUnreadable) as caught:
        push_export(repo)
    assert "unique.txt: its object is missing or is not a blob" in str(caught.value)
    assert remote_refs(remote) == {}


def test_t30_a_grafts_file_does_not_hide_history_from_the_scan(db, tmp_path):
    # Git 2.50 still reads info/grafts; on a git that no longer does, the
    # history is whole anyway and the refusal below still holds.
    repo = committed_repo(tmp_path, db)
    leaked = hand_commit(repo, "add notes", {"notes.txt": wrap(SAMPLE)})
    tip = hand_commit(repo, "remove notes", {"notes.txt": None})
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "grafts").write_text(f"{tip}\n", encoding="ascii")
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportFileCredential) as caught:
        push_export(repo)
    assert f"commit {leaked}: notes.txt" in str(caught.value)
    assert remote_refs(remote) == {}


def test_t31_each_blob_is_checked_once(db, tmp_path, monkeypatch):
    repo = committed_repo(tmp_path, db)
    # Twenty commits that each change only notes.txt, between two contents,
    # so each of its two blobs comes back ten times; then one event file is
    # removed and restored, so its blob comes back too. Every event blob is
    # shared by every commit.
    for i in range(20):
        hand_commit(repo, f"note {i}", {"notes.txt": f"note {i % 2}\n".encode()})
    event_file = sorted(repo.glob("[0-9]*.json"))[0].name
    kept = (repo / event_file).read_bytes()
    hand_commit(repo, "remove an event file", {event_file: None})
    hand_commit(repo, "restore it", {event_file: kept})
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    event_blobs, other_blobs = set(), set()
    for commit in out(repo, "rev-list", "HEAD").split():
        listing = git(repo, "ls-tree", "-r", "-z", commit).stdout
        for entry in filter(None, listing.split(b"\0")):
            header, _tab, path = entry.partition(b"\t")
            oid = header.split()[2].decode()
            name = path.rsplit(b"/", 1)[-1]
            (event_blobs if export_mod._EVENT_NAME_BYTES.match(name) else other_blobs).add(oid)
    # The history lists notes.txt in twenty commits, and the event file in
    # three (added, removed, restored).
    raw_log = out(repo, "log", "--format=", "--raw", "--no-abbrev", "--no-renames", "HEAD")
    assert raw_log.count("\tnotes.txt") == 20
    assert raw_log.count(f"\t{event_file}") == 3
    records, plains = [], []
    real_record, real_plain = export_mod._check_event_record, export_mod._scan_plain_blob

    def record(data):
        records.append(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest())
        return real_record(data)

    def plain(data):
        plains.append(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest())
        return real_plain(data)

    monkeypatch.setattr(export_mod, "_check_event_record", record)
    monkeypatch.setattr(export_mod, "_scan_plain_blob", plain)
    push_export(repo)
    assert remote_refs(remote) == {"refs/heads/main": ref(repo)}
    assert sorted(records) == sorted(event_blobs)
    assert sorted(plains) == sorted(other_blobs)


def test_t32_cli_refusals_exit_3_with_their_codes_and_a_clean_push_reports(
    db, tmp_path, monkeypatch
):
    repo = committed_repo(tmp_path, db)
    (repo / "notes.txt").write_bytes(wrap(SAMPLE))
    text, as_json = cli(monkeypatch, "--db", db, "export", "--dir", str(repo), "--commit")
    assert text[0] == 3 and "export_file_credential" in text[2]
    envelope = json.loads(as_json[1])
    assert as_json[0] == 3 and envelope["error"]["code"] == "export_file_credential"
    assert_no_sample(*text[1:], *as_json[1:])
    (repo / "notes.txt").unlink()

    unused_db = str(tmp_path / "never-opened.db")
    text, as_json = cli(monkeypatch, "--db", unused_db, "export", "--dir", str(repo), "--push")
    assert text[0] == 3 and "export_push_unconfigured" in text[2]
    assert as_json[0] == 3
    assert json.loads(as_json[1])["error"]["code"] == "export_push_unconfigured"

    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    code, body, err = invoke(
        monkeypatch, ["--json", "--db", unused_db, "export", "--dir", str(repo), "--push"]
    )
    assert code == 0, err
    data = json.loads(body)["data"]
    assert data == {"branch": "main", "pushed": True, "remote": "origin", "sha": ref(repo)}
    assert remote_refs(remote) == {"refs/heads/main": ref(repo)}
    code, text_out, err = invoke(
        monkeypatch, ["--db", unused_db, "export", "--dir", str(repo), "--push"]
    )
    assert code == 0, err
    assert text_out.strip() == f"pushed {ref(repo)} to refs/heads/main at origin"
    assert not Path(unused_db).exists(), "export --push opened the ledger"


def _shallow_history(tmp_path: Path, db: str) -> tuple[Path, str, str]:
    repo = committed_repo(tmp_path, db)
    leaked = hand_commit(repo, "add notes", {"notes.txt": wrap(SAMPLE)})
    boundary = hand_commit(repo, "remove notes", {"notes.txt": None})
    hand_commit(repo, "later", {"later.txt": b"later\n"})
    (repo / ".git" / "shallow").write_text(f"{boundary}\n", encoding="ascii")
    return repo, leaked, boundary


def test_t33_a_shallow_repository_refuses(db, tmp_path):
    repo, _leaked, _boundary = _shallow_history(tmp_path, db)
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    with pytest.raises(ExportRepoShallow) as caught:
        push_export(repo)
    assert "export_repo_shallow" in str(caught.value)
    assert remote_refs(remote) == {}


def test_t34_a_parent_missing_from_the_listing_refuses(db, tmp_path, monkeypatch):
    repo, leaked, boundary = _shallow_history(tmp_path, db)
    remote = with_remote(repo, bare(tmp_path / "remote.git"))

    def hook(args, run):
        if args == ["rev-parse", "--is-shallow-repository"]:
            return subprocess.CompletedProcess(args, 0, b"false\n", b"")
        return run()

    wrap_git(monkeypatch, hook)
    with pytest.raises(ExportHistoryIncomplete) as caught:
        push_export(repo)
    assert f"commit {boundary} names parent {leaked}" in str(caught.value)
    assert remote_refs(remote) == {}


def test_t35_head_switched_before_the_swap_refuses_and_only_main_could_move(
    db, tmp_path, monkeypatch
):
    repo = committed_repo(tmp_path, db)
    seed(monkeypatch, db, "it-2")
    export(db, repo)
    old = ref(repo)
    index_before = (repo / ".git" / "index").read_bytes()

    def hook(args, run):
        result = run()
        if args[0] == "commit-tree":
            git(repo, "branch", "other", old)
            git(repo, "symbolic-ref", "HEAD", "refs/heads/other")
        return result

    wrap_git(monkeypatch, hook)
    with pytest.raises(ExportHeadMoved):
        commit_export(repo)
    assert ref(repo, "refs/heads/main") == old
    assert ref(repo, "refs/heads/other") == old
    assert out(repo, "symbolic-ref", "HEAD") == "refs/heads/other"
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_t36_unborn_with_head_switched_before_the_swap_creates_nothing(
    db, tmp_path, monkeypatch
):
    repo = exported_repo(tmp_path, db)

    def hook(args, run):
        result = run()
        if args[0] == "commit-tree":
            git(repo, "symbolic-ref", "HEAD", "refs/heads/elsewhere")
        return result

    wrap_git(monkeypatch, hook)
    with pytest.raises(ExportHeadMoved):
        commit_export(repo)
    assert ref(repo, "refs/heads/main") is None
    assert ref(repo, "refs/heads/elsewhere") is None


@pytest.mark.parametrize("case", ["a-two-pushurls", "b-two-urls", "c-url-and-pushurl"])
def test_t37_the_push_url_count_with_configuration_fixed_beforehand(db, tmp_path, case):
    repo = committed_repo(tmp_path, db)
    first, second = bare(tmp_path / "first.git"), bare(tmp_path / "second.git")
    git(repo, "remote", "add", "origin", str(first))
    git(repo, "config", "branch.main.remote", "origin")
    if case == "a-two-pushurls":
        git(repo, "config", "--add", "remote.origin.pushurl", str(first))
        git(repo, "config", "--add", "remote.origin.pushurl", str(second))
    elif case == "b-two-urls":
        git(repo, "config", "--add", "remote.origin.url", str(second))
    else:
        git(repo, "config", "remote.origin.pushurl", str(second))
    if case == "c-url-and-pushurl":
        pushed = push_export(repo)
        assert remote_refs(second) == {"refs/heads/main": pushed.sha}
        assert remote_refs(first) == {}
        return
    with pytest.raises(ExportPushMultipleUrls) as caught:
        push_export(repo)
    assert "export_push_multiple_urls" in str(caught.value)
    assert remote_refs(first) == {} and remote_refs(second) == {}


def test_t38_a_push_that_fails_after_it_started_is_outcome_unknown(db, tmp_path, monkeypatch):
    repo = committed_repo(tmp_path, db)
    remote = bare(tmp_path / "remote.git")
    other = init_repo(tmp_path / "other")
    hand_commit(other, "unrelated", {"u.txt": b"u\n"})
    git(other, "push", "-q", str(remote), "main")
    with_remote(repo, remote)
    sha = ref(repo)
    text, as_json = cli(monkeypatch, "--db", str(tmp_path / "none.db"), "export",
                        "--dir", str(repo), "--push")
    assert text[0] == 3 and as_json[0] == 3
    envelope = json.loads(as_json[1])
    assert envelope["error"]["code"] == "export_push_failed"
    message = envelope["error"]["message"]
    assert "the outcome is unknown" in message
    assert f"git push of {sha} to refs/heads/main" in message
    with pytest.raises(ExportPushFailed):
        push_export(repo)


def test_t39_the_index_is_left_alone_when_head_moved_after_the_swap(
    db, tmp_path, monkeypatch
):
    repo = committed_repo(tmp_path, db)
    seed(monkeypatch, db, "it-2")
    export(db, repo)
    old = ref(repo)
    index_before = (repo / ".git" / "index").read_bytes()
    state = {"phase": 0}

    def hook(args, run):
        # phase 1: commit-tree ran; phase 2: the swap's HEAD check ran; the
        # next git call is the first one after the swap committed.
        if state["phase"] == 2:
            git(repo, "branch", "other", old)
            git(repo, "symbolic-ref", "HEAD", "refs/heads/other")
            state["phase"] = 3
        result = run()
        if args[0] == "commit-tree":
            state["phase"] = 1
        elif state["phase"] == 1 and args[0] == "symbolic-ref":
            state["phase"] = 2
        return result

    wrap_git(monkeypatch, hook)
    result = commit_export(repo)
    assert state["phase"] == 3
    assert result.sha is not None and ref(repo) == result.sha
    assert result.index_refreshed is False
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_t41a_a_branch_name_holding_a_shape_refuses_the_push(db, tmp_path, monkeypatch):
    branch = f"fix/{SAMPLE_TEXT}"
    repo = exported_repo(tmp_path, db, branch=branch)
    hand_commit(repo, "export", {})
    remote = with_remote(repo, bare(tmp_path / "remote.git"), branch=branch)
    text, as_json = cli(monkeypatch, "--db", str(tmp_path / "none.db"), "export",
                        "--dir", str(repo), "--push")
    assert text[0] == 3 and "export_branch_credential" in text[2]
    envelope = json.loads(as_json[1])
    assert as_json[0] == 3 and envelope["error"]["code"] == "export_branch_credential"
    assert envelope["data"] is None and "branch" not in json.dumps(envelope["data"])
    assert_no_sample(*text[1:], *as_json[1:])
    assert remote_refs(remote) == {}


def test_t41b_a_branch_name_holding_a_shape_refuses_the_commit(db, tmp_path, monkeypatch):
    branch = f"fix/{SAMPLE_TEXT}"
    repo = init_repo(tmp_path / "export", branch)
    gitdir_before = sorted(os.listdir(repo / ".git"))
    text, as_json = cli(monkeypatch, "--db", db, "export", "--dir", str(repo), "--commit")
    assert text[0] == 3 and "export_branch_credential" in text[2]
    assert json.loads(as_json[1])["error"]["code"] == "export_branch_credential"
    assert_no_sample(*text[1:], *as_json[1:])
    assert ref(repo, f"refs/heads/{branch}") is None
    assert sorted(os.listdir(repo / ".git")) == gitdir_before


def test_t42_a_failed_signature_refuses_the_commit(db, tmp_path, monkeypatch, isolated_git):
    # The global config says false and the repository says true: git's own
    # scope order makes the repository value win, so the commit must sign.
    assert "gpgSign = false" in isolated_git.read_text(encoding="utf-8")
    repo = committed_repo(tmp_path, db)
    assert out(repo, "config", "--global", "--get", "commit.gpgSign") == "false"
    failing = executable(tmp_path / "gpg-fail.sh", "#!/bin/sh\ncat > /dev/null\nexit 1\n")
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", str(failing))
    seed(monkeypatch, db, "it-2")
    old = ref(repo)
    index_before = (repo / ".git" / "index").read_bytes()
    text = invoke(monkeypatch, ["--db", db, "export", "--dir", str(repo), "--commit"])
    assert text[0] == 3 and "export_commit_sign_failed" in text[2]
    seed(monkeypatch, db, "it-3")
    code, body, _err = invoke(
        monkeypatch, ["--json", "--db", db, "export", "--dir", str(repo), "--commit"]
    )
    assert code == 3
    assert json.loads(body)["error"]["code"] == "export_commit_sign_failed"
    assert ref(repo) == old
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_t43_a_signing_program_that_succeeds_signs_the_commit(db, tmp_path):
    repo = exported_repo(tmp_path, db)
    signer = executable(
        tmp_path / "gpg-ok.sh",
        "#!/bin/sh\ncat > /dev/null\n"
        "printf '[GNUPG:] BEGIN_SIGNING\\n[GNUPG:] SIG_CREATED D 1 8 00 1700000000 ABCDEF\\n' >&2\n"
        "printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nZmFrZQ==\\n-----END PGP SIGNATURE-----\\n'\n",
    )
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", str(signer))
    result = commit_export(repo)
    assert result.sha is not None and ref(repo) == result.sha
    header = git(repo, "cat-file", "commit", result.sha).stdout.split(b"\n\n", 1)[0]
    assert b"\ngpgsig -----BEGIN PGP SIGNATURE-----" in header


# ---- push certificate, deep nesting, a branch name that is not UTF-8 ---------------


def test_t44_push_gpg_sign_sends_no_push_certificate(db, tmp_path):
    # A push certificate names the pusher (the committer ident) and is sent
    # beside the commits, so nothing in the scanned history covers it. The
    # remote advertises certificates and the repository asks to sign every
    # push; the receiving hook records any certificate that arrives.
    repo = committed_repo(tmp_path, db)
    remote = with_remote(repo, bare(tmp_path / "remote.git"))
    signer = executable(
        tmp_path / "gpg-ok.sh",
        "#!/bin/sh\ncat > /dev/null\n"
        "printf '[GNUPG:] BEGIN_SIGNING\\n[GNUPG:] SIG_CREATED D 1 8 00 1700000000 ABCDEF\\n' >&2\n"
        "printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nZmFrZQ==\\n-----END PGP SIGNATURE-----\\n'\n",
    )
    git(remote, "config", "receive.certNonceSeed", "pushscan-test-seed")
    git(remote, "config", "gpg.program", str(signer))
    record = tmp_path / "push-cert.txt"
    executable(
        remote / "hooks" / "pre-receive",
        "#!/bin/sh\ncat > /dev/null\n"
        f"if [ -n \"$GIT_PUSH_CERT\" ]; then git cat-file blob \"$GIT_PUSH_CERT\" > '{record}'; "
        f"else echo none > '{record}'; fi\n",
    )
    git(repo, "config", "gpg.program", str(signer))
    git(repo, "config", "push.gpgSign", "true")
    # The clean commit exists; only now does the ident take a credential shape.
    git(repo, "config", "user.name", SAMPLE_TEXT)
    pushed = push_export(repo)
    assert remote_refs(remote) == {"refs/heads/main": pushed.sha}
    assert record.read_text(encoding="utf-8") == "none\n", "the push sent a push certificate"


def _deep_event_bytes(depth: int = 2000) -> bytes:
    """An event file whose payload nests ``depth`` objects: too deep to
    canonicalise. Python 3.12 and later parse it; Python 3.11's parser
    shares the recursion limit and refuses it while parsing."""
    record = _record_dict(doc_event(HAND_ID, b"plain notes\n"))
    record["payload"] = {"nest": 0}
    text = canonical_dumps(record)
    assert text.count('{"nest":0}') == 1
    nested = '{"a":' * depth + "0" + "}" * depth
    return (text.replace('{"nest":0}', nested) + "\n").encode("utf-8")


@pytest.mark.parametrize("step", ["commit", "push"])
def test_t45_a_deeply_nested_event_file_refuses_as_invalid(db, tmp_path, step):
    # The check that fails is the first one that recurses too deep on this
    # interpreter: canonicalising where the parser copes, else parsing.
    try:
        json.loads(_deep_event_bytes())
        check = "canonical bytes"
    except RecursionError:
        check = "json"
    repo = committed_repo(tmp_path, db)
    if step == "commit":
        (repo / EVENT_NAME).write_bytes(_deep_event_bytes())
        before = snapshot(repo)
        with pytest.raises(ExportEventInvalid) as caught:
            commit_export(repo)
        assert snapshot(repo) == before
    else:
        hand_commit(repo, "add a deep event", {EVENT_NAME: _deep_event_bytes()})
        remote = with_remote(repo, bare(tmp_path / "remote.git"))
        with pytest.raises(ExportEventInvalid) as caught:
            push_export(repo)
        assert remote_refs(remote) == {}
    message = str(caught.value)
    assert "export_event_invalid" in message and EVENT_NAME in message
    assert f"(failed check: {check})" in message
    if sys.version_info[:2] == (3, 11):
        assert check == "json"


def test_t46_a_branch_name_that_is_not_utf8_commits(db, tmp_path, monkeypatch):
    # A reftable repository keeps its refs inside its table files, so the
    # branch name needs no file of its own (a file system may refuse a file
    # name that is not UTF-8). The branch is made with git update-ref.
    repo = tmp_path / "export"
    subprocess.run(
        ["git", "init", "-q", "--ref-format=reftable", "-b", "main", str(repo)],
        check=True, capture_output=True,
    )
    git(repo, "config", "user.name", "ledger-test")
    git(repo, "config", "user.email", "ledger-test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    export(db, repo)
    old = commit_export(repo).sha
    assert old is not None
    branch = "refs/heads/" + os.fsdecode(b"caf\xe9-\xff")
    git(repo, "update-ref", branch, old)
    git(repo, "symbolic-ref", "HEAD", branch)
    assert git(repo, "symbolic-ref", "HEAD").stdout == b"refs/heads/caf\xe9-\xff\n"
    seed(monkeypatch, db, "it-2")
    export(db, repo)
    result = commit_export(repo)
    assert result.sha is not None and result.index_refreshed is True
    assert ref(repo, branch) == result.sha
    assert ref(repo) == old
    assert out(repo, "rev-list", "--parents", "-n", "1", result.sha).split() == [result.sha, old]


# ---- other failures ----------------------------------------------------------------


def test_a_git_failure_in_the_commit_steps_is_export_git_failed(db, tmp_path, monkeypatch):
    for name in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "export"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.useConfigOnly", "true")
    export(db, repo)
    before = snapshot(repo)
    with pytest.raises(ExportGitFailed) as caught:
        commit_export(repo)
    assert "export_git_failed: git commit-tree failed" in str(caught.value)
    assert snapshot(repo) == before


def test_git_output_holding_a_shape_is_withheld(db, tmp_path):
    repo = committed_repo(tmp_path, db)
    missing = tmp_path / f"{SAMPLE_TEXT}.git"
    git(repo, "config", "branch.main.remote", str(missing))
    with pytest.raises(ExportPushFailed) as caught:
        push_export(repo)
    message = str(caught.value)
    assert "git output withheld: it holds a credential shape (openai-key)" in message
    assert_no_sample(message)
