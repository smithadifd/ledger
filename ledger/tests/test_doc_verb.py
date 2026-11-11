"""Documents (LC5): ``doc.attached`` and ``ledger doc add|show|verify``.

A document is a content-addressed record on an item: the ledger computes its
sha256 and size from the bytes it read and stores the bytes inline. Documents
are inline-only, so no read after ``doc add`` touches the filesystem, and
``doc add`` refuses a file whose path holds a symlink anywhere. Nothing is
printed as a document's content, or reported ``ok``, unless its stored bytes
hash to the stored sha256. Only ``attach_document`` writes the kind; restore
and replay recompute the digest before inserting it and refuse a ``path``.
"""
from __future__ import annotations

import base64
import dataclasses
import errno
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.export import event_filename, event_record, export_events, hash_body
from ledger.restore import replay_events, restore_export
from ledger.schema import DOC_INLINE_LIMIT, DOC_KIND, ValidationError, validate_payload
from ledger.store import LedgerError, canonical_dumps, event_digest, open_ledger
from ledger.verbs import doc as doc_verb

from test_cli import invoke, seed

ITEM = "it-1"
PKG_PARENT = Path(__file__).resolve().parent.parent.parent
BINARY = b"\x00head\xff\x00\r\n\xfe\xff tail\x00"
ROW_KEYS = {
    "event_id", "item_id", "role", "title", "mime", "size", "sha256", "storage", "path",
}
VERIFY_ROW_KEYS = {"event_id", "item_id", "sha256", "status"}
ENVELOPE_KEYS = {"ok", "data", "error"}
PAYLOAD_KEYS = {"role", "sha256", "size", "mime", "content_b64"}


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    home = tmp_path / "home"
    home.mkdir()
    path = str(home / "ledger.db")
    seed(monkeypatch, path, ITEM)
    return path


def cli(monkeypatch, db: str, *argv: str):
    return invoke(monkeypatch, ["--db", db, *argv])


def cli_json(monkeypatch, db: str, *argv: str):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, *argv])
    return code, (json.loads(out) if out.strip() else None), err


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def add(monkeypatch, db: str, file: Path | str, *extra: str, role: str = "brief"):
    return cli_json(monkeypatch, db, "doc", "add", ITEM, str(file), "--role", role, *extra)


def doc_events(db: str) -> list:
    with open_ledger(db, create=False) as lg:
        return lg.events(kind=DOC_KIND)


def run_module(
    db: str, *argv: str, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """The real CLI in a child process, stdout captured as bytes."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.pop("PYTHONPATH", None)
    env["LEDGER_HARNESS"] = "pytest"
    return subprocess.run(
        [sys.executable, "-m", "ledger", "--db", db, *argv],
        cwd=PKG_PARENT, capture_output=True, env=env, check=False, timeout=timeout,
    )


def test_the_test_directory_holds_no_symlink(tmp_path):
    # Every add below passes a tmp_path file; the symlink rule would refuse
    # them all if the temporary root itself went through a symlink.
    assert os.path.realpath(tmp_path) == os.path.abspath(tmp_path)


# ---- add, show and the content round trip -------------------------------------


def test_add_inline_then_show_then_content_gives_the_exact_bytes(db, tmp_path, monkeypatch):
    body = b"# Brief\n\nBuild the thing.\n"
    source = write(tmp_path / "brief.md", body)
    code, added, err = add(monkeypatch, db, source, "--title", "The brief")
    assert code == 0, err
    document = added["data"]["document"]
    assert added["data"]["unchanged"] is False
    assert document["storage"] == "inline" and document["path"] is None
    assert document["mime"] == "text/markdown"
    assert document["size"] == len(body)
    assert document["sha256"] == hashlib.sha256(body).hexdigest()

    code, shown, err = cli_json(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    assert shown["data"]["documents"] == [document]

    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    assert out == (
        f"{document['event_id']}\tbrief\t{len(body)}\t{document['sha256'][:12]}\t"
        "inline\tThe brief\n"
    )

    code, out, err = cli(
        monkeypatch, db, "doc", "show", ITEM, "--id", str(document["event_id"]), "--content"
    )
    assert code == 0, err
    assert out == body.decode("utf-8")


def test_a_file_with_no_md_suffix_defaults_to_octet_stream(db, tmp_path, monkeypatch):
    code, added, err = add(monkeypatch, db, write(tmp_path / "blob.bin", b"xyz"))
    assert code == 0, err
    assert added["data"]["document"]["mime"] == "application/octet-stream"
    code, added, err = add(
        monkeypatch, db, write(tmp_path / "other.bin", b"abc"), "--mime", "text/plain"
    )
    assert code == 0, err
    assert added["data"]["document"]["mime"] == "text/plain"


def test_show_lists_documents_oldest_first_and_id_shows_one(db, tmp_path, monkeypatch):
    first = add(monkeypatch, db, write(tmp_path / "a.txt", b"one"), role="brief")[1]
    second = add(monkeypatch, db, write(tmp_path / "b.txt", b"two"), role="retro")[1]
    code, shown, err = cli_json(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    ids = [row["event_id"] for row in shown["data"]["documents"]]
    assert ids == [first["data"]["document"]["event_id"], second["data"]["document"]["event_id"]]
    assert ids == sorted(ids)
    wanted = str(ids[1])
    code, shown, err = cli_json(monkeypatch, db, "doc", "show", ITEM, "--id", wanted)
    assert code == 0, err
    assert [row["role"] for row in shown["data"]["documents"]] == ["retro"]


def test_show_of_an_item_with_no_documents_is_empty_and_exit_0(db, monkeypatch):
    code, shown, err = cli_json(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    assert shown == {"ok": True, "error": None,
                     "data": {"item_id": ITEM, "documents": []}}


# ---- the bytes are inline: no read after add touches the filesystem -----------


def test_show_content_and_verify_succeed_after_the_source_file_is_deleted(
    db, tmp_path, monkeypatch
):
    body = b"the packet, version one\n"
    source = write(tmp_path / "packet.txt", body)
    code, added, err = add(monkeypatch, db, source, role="packet")
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    assert set(doc_events(db)[0].payload) == PAYLOAD_KEYS

    # A changed source is not the document: its stored bytes are.
    source.write_bytes(b"the packet, version two\n")
    code, out, err = cli(monkeypatch, db, "doc", "verify", ITEM)
    assert (code, out) == (0, "ok documents=1\n"), err
    source.unlink()
    code, out, err = cli(monkeypatch, db, "doc", "verify", ITEM)
    assert (code, out) == (0, "ok documents=1\n"), err
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify", ITEM, "--id", event_id)
    assert code == 0, err
    assert [row["status"] for row in verified["data"]["documents"]] == ["ok"]
    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert (code, out) == (0, body.decode()), err
    shown = run_module(db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert (shown.returncode, shown.stdout) == (0, body), shown.stderr


# ---- idempotency ---------------------------------------------------------------


def test_re_adding_identical_content_writes_nothing(db, tmp_path, monkeypatch):
    source = write(tmp_path / "brief.md", b"same bytes")
    code, first, err = add(monkeypatch, db, source)
    assert code == 0, err
    code, second, err = add(monkeypatch, db, source, "--title", "a new title")
    assert code == 0, err
    assert second["data"]["unchanged"] is True
    assert second["data"]["document"]["event_id"] == first["data"]["document"]["event_id"]
    code, out, err = cli(monkeypatch, db, "doc", "add", ITEM, str(source), "--role", "brief")
    assert code == 0, err
    assert out.endswith(f"unchanged event={first['data']['document']['event_id']}\n")
    assert len(doc_events(db)) == 1


def test_the_same_bytes_under_another_role_are_a_second_document(db, tmp_path, monkeypatch):
    source = write(tmp_path / "doc.md", b"shared bytes")
    code, first, err = add(monkeypatch, db, source, role="brief")
    assert code == 0, err
    code, second, err = add(monkeypatch, db, source, role="retro")
    assert code == 0, err
    assert second["data"]["unchanged"] is False
    assert second["data"]["document"]["event_id"] != first["data"]["document"]["event_id"]
    assert [event.payload["role"] for event in doc_events(db)] == ["brief", "retro"]


def test_the_idempotency_key_is_derived_from_item_role_and_sha256(db, tmp_path, monkeypatch):
    body = b"keyed bytes"
    code, _added, err = add(monkeypatch, db, write(tmp_path / "k.txt", body), role="other")
    assert code == 0, err
    [event] = doc_events(db)
    sha = hashlib.sha256(body).hexdigest()
    assert event.idempotency_key == f"doc:{ITEM}:other:{sha}"


# ---- the inline limit ----------------------------------------------------------


def test_over_the_inline_limit_refuses_and_writes_nothing(db, tmp_path, monkeypatch):
    big = write(tmp_path / "big.bin", b"x" * (DOC_INLINE_LIMIT + 1))
    code, refused, _err = add(monkeypatch, db, big)
    assert code == 2
    assert refused["error"]["code"] == "usage"
    assert "over the 65536-byte inline limit" in refused["error"]["message"]
    assert "by-path documents are not supported yet" in refused["error"]["message"]
    assert doc_events(db) == []


def test_at_the_limit_is_stored_inline(db, tmp_path, monkeypatch):
    exact = write(tmp_path / "exact.bin", b"y" * DOC_INLINE_LIMIT)
    code, added, err = add(monkeypatch, db, exact)
    assert code == 0, err
    assert added["data"]["document"]["storage"] == "inline"
    assert added["data"]["document"]["size"] == DOC_INLINE_LIMIT
    [event] = doc_events(db)
    assert base64.b64decode(event.payload["content_b64"]) == b"y" * DOC_INLINE_LIMIT


# ---- the reserved kind ---------------------------------------------------------


def _forged_payload() -> dict:
    return {"role": "brief", "sha256": "0" * 64, "size": 3, "mime": "text/plain",
            "content_b64": base64.b64encode(b"abc").decode("ascii")}


def test_generic_append_and_append_many_refuse_doc_attached(db, monkeypatch):
    payload = json.dumps(_forged_payload())
    code, refused, _err = cli_json(
        monkeypatch, db, "append", "--kind", DOC_KIND, "--item", ITEM, "--payload", payload
    )
    assert code == 3
    assert refused["error"]["message"] == "reserved-kind:doc.attached"
    batch = json.dumps([{"kind": DOC_KIND, "payload": _forged_payload()}])
    monkeypatch.setattr(sys, "stdin", io.StringIO(batch))
    code, refused, _err = cli_json(monkeypatch, db, "append-many", ITEM)
    assert code == 3
    assert "reserved-kind:doc.attached" in refused["error"]["message"]
    with open_ledger(db, create=False) as lg:
        with pytest.raises(ValidationError, match="reserved-kind:doc.attached"):
            lg.append(kind=DOC_KIND, payload=_forged_payload(), item_id=ITEM,
                      idempotency_key="forged")
    assert doc_events(db) == []


def test_the_doc_writer_path_needs_its_capability(db):
    with open_ledger(db, create=False) as lg:
        with pytest.raises(ValidationError, match="doc-writer-capability-required"):
            lg._append_doc_event(object(), kind=DOC_KIND, payload=_forged_payload(),
                                 item_id=ITEM, idempotency_key="forged")
        with pytest.raises(ValidationError, match="reserved-kind:item.spec"):
            lg._append_doc_event(doc_verb._DOC_WRITER_CAPABILITY, kind="item.spec",
                                 payload={}, item_id=ITEM, idempotency_key="forged")
    assert doc_events(db) == []


# ---- refusals ------------------------------------------------------------------


def test_an_unknown_item_refuses_not_found(db, tmp_path, monkeypatch):
    source = write(tmp_path / "a.txt", b"a")
    for argv in (
        ("doc", "add", "it-nope", str(source), "--role", "brief"),
        ("doc", "show", "it-nope"),
        ("doc", "verify", "it-nope"),
    ):
        code, refused, _err = cli_json(monkeypatch, db, *argv)
        assert code == 1, argv
        assert refused["error"]["code"] == "not_found", argv
    assert doc_events(db) == []


def test_an_unknown_document_id_refuses_not_found(db, tmp_path, monkeypatch):
    code, refused, _err = cli_json(monkeypatch, db, "doc", "show", ITEM, "--id", "999")
    assert (code, refused["error"]["code"]) == (1, "not_found")
    code, refused, _err = cli_json(monkeypatch, db, "doc", "verify", "--id", "999")
    assert (code, refused["error"]["code"]) == (1, "not_found")


def test_content_without_id_and_bad_flags_are_usage_errors(db, tmp_path, monkeypatch):
    source = write(tmp_path / "a.txt", b"a")
    for argv in (
        ("doc", "show", ITEM, "--content"),
        ("doc", "show", ITEM, "--id", "0"),
        ("doc", "show", ITEM, "--role", "brief"),
        ("doc", "verify", ITEM, "--content"),
        ("doc", "add", ITEM, str(source)),
        ("doc", "add", ITEM, str(source), "--role", "nonsense"),
        # Documents are inline-only: the storage flags are gone.
        ("doc", "add", ITEM, str(source), "--role", "brief", "--by-path"),
        ("doc", "add", ITEM, str(source), "--role", "brief", "--inline"),
    ):
        code, refused, _err = cli_json(monkeypatch, db, *argv)
        assert code == 2, argv
        assert refused["error"]["code"] == "usage", argv
    assert doc_events(db) == []


def test_an_unreadable_input_file_is_refused(db, tmp_path, monkeypatch):
    for target in (tmp_path / "does-not-exist", tmp_path):
        code, refused, _err = add(monkeypatch, db, target)
        assert code == 3, target
        assert refused["error"]["code"] == "doc_unreadable", target
    assert doc_events(db) == []


def test_a_control_character_in_the_title_is_refused(db, tmp_path, monkeypatch):
    source = write(tmp_path / "a.txt", b"a")
    code, refused, _err = add(monkeypatch, db, source, "--title", "evil \x1b[2J title")
    assert code == 3
    assert refused["error"]["message"] == "ask_control_character:title"
    assert doc_events(db) == []


def test_the_schema_requires_inline_bytes_and_refuses_a_path():
    base = {"role": "brief", "sha256": "a" * 64, "size": 0, "mime": "text/plain"}
    validate_payload(DOC_KIND, {**base, "content_b64": ""})
    with pytest.raises(ValidationError, match="missing-key:content_b64"):
        validate_payload(DOC_KIND, base)
    for payload in ({**base, "path": "/x"}, {**base, "content_b64": "", "path": "/x"}):
        with pytest.raises(ValidationError, match="unknown-key:path"):
            validate_payload(DOC_KIND, payload)


# ---- the chain check -----------------------------------------------------------


def test_ledger_verify_passes_on_a_ledger_holding_documents(db, tmp_path, monkeypatch):
    assert add(monkeypatch, db, write(tmp_path / "a.md", b"inline"))[0] == 0
    assert add(monkeypatch, db, write(tmp_path / "b.bin", BINARY), role="packet")[0] == 0
    code, out, err = cli(monkeypatch, db, "verify")
    assert code == 0, err
    assert out.startswith("ok events=")


# ---- restore and replay recompute the inline digest ---------------------------


def _forge_inline(db: str, *, sha256: str | None = None, size: int | None = None) -> None:
    """Write an inline document whose stored digest is not its bytes' (a store
    a hand-edited writer left behind), through the writer's own capability."""
    body = b"real bytes"
    payload = {
        "role": "brief",
        "sha256": sha256 if sha256 is not None else hashlib.sha256(body).hexdigest(),
        "size": size if size is not None else len(body),
        "mime": "text/plain",
        "content_b64": base64.b64encode(body).decode("ascii"),
    }
    with open_ledger(db, create=False) as lg:
        lg._append_doc_event(doc_verb._DOC_WRITER_CAPABILITY, kind=DOC_KIND,
                             payload=payload, item_id=ITEM, idempotency_key="forged")


@pytest.mark.parametrize("forged", [{"sha256": "f" * 64}, {"size": 3}])
def test_restore_refuses_an_inline_document_whose_digest_is_not_its_bytes(
    db, tmp_path, forged
):
    _forge_inline(db, **forged)
    with open_ledger(db, create=False) as lg:
        export_events(lg, tmp_path / "export")
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises(
        LedgerError,
        match=r"restore refuses event \d+ \(doc\.attached\): restore_doc_digest_mismatch",
    ):
        restore_export(tmp_path / "export", dest, system="home")
    assert not any(dest.parent.glob("ledger.db*")), "a refused restore left files behind"


@pytest.mark.parametrize("forged", [{"sha256": "f" * 64}, {"size": 3}])
def test_replay_events_refuses_the_same_forged_document(db, tmp_path, forged):
    _forge_inline(db, **forged)
    with open_ledger(db, create=False) as lg:
        events = lg.events()
    with open_ledger(tmp_path / "fresh.db") as fresh:
        with pytest.raises(LedgerError, match="restore_doc_digest_mismatch"):
            replay_events(fresh, events)
        assert fresh.events() == [], "a refused replay inserted events"


def test_replay_refuses_an_inline_document_over_the_limit(db, tmp_path):
    body = b"q" * (DOC_INLINE_LIMIT + 1)
    payload = {"role": "packet", "sha256": hashlib.sha256(body).hexdigest(),
               "size": len(body), "mime": "text/plain",
               "content_b64": base64.b64encode(body).decode("ascii")}
    with open_ledger(db, create=False) as lg:
        lg._append_doc_event(doc_verb._DOC_WRITER_CAPABILITY, kind=DOC_KIND,
                             payload=payload, item_id=ITEM, idempotency_key="too-big")
        events = lg.events()
    with open_ledger(tmp_path / "fresh.db") as fresh:
        with pytest.raises(
            LedgerError,
            match="restore_doc_digest_mismatch: 65537 inline bytes, over the 65536-byte limit",
        ):
            replay_events(fresh, events)
        assert fresh.events() == []


def test_replay_refuses_non_canonical_or_malformed_inline_bytes(db, tmp_path):
    _forge_inline(db)
    with open_ledger(db, create=False) as lg:
        events = lg.events()
    [document] = [event for event in events if event.kind == DOC_KIND]
    # unpadded, not base64, trailing space, and non-canonical (unused bits set)
    encodings = ("cmVhbCBieXRlcw", "!!!", "cmVhbCBieXRlcw== ", "cmVhbCBieXRlcx==")
    assert base64.b64decode(encodings[3]) == b"real bytes"
    for index, encoded in enumerate(encodings):
        bad = dataclasses.replace(
            document, payload={**document.payload, "content_b64": encoded}
        )
        replaced = [bad if event.id == document.id else event for event in events]
        with open_ledger(tmp_path / f"fresh-{index}.db") as fresh:
            with pytest.raises(LedgerError, match="restore_doc_digest_mismatch"):
                replay_events(fresh, replaced)
            assert fresh.events() == []


def test_a_correct_inline_document_restores(db, tmp_path, monkeypatch):
    assert add(monkeypatch, db, write(tmp_path / "in.md", b"kept inline"))[0] == 0
    assert add(monkeypatch, db, write(tmp_path / "bin.bin", BINARY), role="packet")[0] == 0
    with open_ledger(db, create=False) as lg:
        export_events(lg, tmp_path / "export")
    dest = tmp_path / "restored" / "ledger.db"
    restore_export(tmp_path / "export", dest, system="home")
    code, verified, err = cli_json(monkeypatch, str(dest), "doc", "verify", ITEM)
    assert code == 0, err
    assert [row["status"] for row in verified["data"]["documents"]] == ["ok", "ok"]
    assert [event.payload for event in doc_events(str(dest))] == [
        event.payload for event in doc_events(db)
    ]


# ---- a path field is refused everywhere: at add, on restore, on replay --------


def test_an_event_carrying_a_path_cannot_be_built_at_add(db, tmp_path, monkeypatch):
    # doc add stores no path (whatever FILE is named), and the writer cannot
    # be given one.
    evil = write(tmp_path / "evil\x1b[2Jname.txt", b"bytes")
    code, added, err = add(monkeypatch, db, evil)
    assert code == 0, err
    [stored] = doc_events(db)
    assert set(stored.payload) == PAYLOAD_KEYS
    assert "evil" not in json.dumps(stored.payload)
    with open_ledger(db, create=False) as lg:
        with pytest.raises(ValidationError, match="unknown-key:path"):
            lg._append_doc_event(
                doc_verb._DOC_WRITER_CAPABILITY, kind=DOC_KIND,
                payload={**stored.payload, "path": str(evil)}, item_id=ITEM,
                idempotency_key="with-a-path",
            )
    assert len(doc_events(db)) == 1


@pytest.mark.parametrize("shape", ["inline-and-path", "path-only"])
def test_a_restore_or_replay_of_a_document_carrying_a_path_is_refused(
    db, tmp_path, monkeypatch, shape
):
    assert add(monkeypatch, db, write(tmp_path / "clean.txt", b"clean"))[0] == 0
    # The last event of a valid export, a path added (and, for path-only, the
    # inline bytes dropped, as a by-path document had them) and its hash
    # recomputed, so the chain still verifies.
    export = tmp_path / "export"
    with open_ledger(db, create=False) as lg:
        export_events(lg, export)
        events = lg.events()
    document = events[-1]
    assert document.kind == DOC_KIND
    payload = {**document.payload, "path": str(tmp_path / "elsewhere.txt")}
    if shape == "path-only":
        del payload["content_b64"]
    forged = dataclasses.replace(document, payload=payload)
    forged = dataclasses.replace(forged, hash=event_digest(hash_body(forged)))
    (export / event_filename(forged.id)).write_text(
        canonical_dumps(event_record(forged)) + "\n", encoding="utf-8"
    )
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises((ValidationError, LedgerError), match="unknown-key:path"):
        restore_export(export, dest, system="home")
    assert not any(dest.parent.glob("ledger.db*")), "a refused restore left files behind"
    replaced = [forged if event.id == forged.id else event for event in events]
    with open_ledger(tmp_path / "fresh.db") as fresh:
        with pytest.raises(
            LedgerError,
            match=r"restore refuses event \d+ \(doc\.attached\): unknown-key:path",
        ):
            replay_events(fresh, replaced)
        assert fresh.events() == [], "a refused replay inserted events"


# ---- output: raw bytes and the pinned envelopes -------------------------------


def test_show_content_writes_exactly_the_stored_bytes_binary_safe(db, tmp_path, monkeypatch):
    source = write(tmp_path / "binary.bin", BINARY)
    code, added, err = add(monkeypatch, db, source)
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    shown = run_module(db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout == BINARY

    code, shown, err = cli_json(monkeypatch, db, "doc", "show", ITEM, "--id", event_id,
                                "--content")
    assert code == 0, err
    assert set(shown) == ENVELOPE_KEYS
    assert set(shown["data"]) == {"document"}
    assert set(shown["data"]["document"]) == ROW_KEYS | {"content_b64"}
    assert base64.b64decode(shown["data"]["document"]["content_b64"]) == BINARY


def test_the_show_and_verify_json_shapes_are_pinned(db, tmp_path, monkeypatch):
    assert add(monkeypatch, db, write(tmp_path / "a.md", b"a"))[0] == 0
    code, shown, err = cli_json(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    assert set(shown) == ENVELOPE_KEYS
    assert set(shown["data"]) == {"item_id", "documents"}
    assert [set(row) for row in shown["data"]["documents"]] == [ROW_KEYS]
    [row] = shown["data"]["documents"]
    assert (row["storage"], row["path"]) == ("inline", None)
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify")
    assert code == 0, err
    assert verified["ok"] is True and verified["error"] is None
    assert set(verified["data"]) == {"documents"}
    assert [set(row) for row in verified["data"]["documents"]] == [VERIFY_ROW_KEYS]


def test_a_failed_doc_verify_reads_ok_false_exit_4_with_every_row(db, tmp_path, monkeypatch):
    assert add(monkeypatch, db, write(tmp_path / "kept.txt", b"unchanged"))[0] == 0
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify")
    assert code == 0, err
    assert verified["ok"] is True and verified["error"] is None

    _forge_inline(db, sha256="f" * 64)
    code, verified, _err = cli_json(monkeypatch, db, "doc", "verify")
    assert code == 4
    assert verified["ok"] is False
    assert verified["error"]["code"] == "doc_verify_failed"
    assert "1 of 2 documents" in verified["error"]["message"]
    assert [row["status"] for row in verified["data"]["documents"]] == ["ok", "mismatch"]
    assert [set(row) for row in verified["data"]["documents"]] == [VERIFY_ROW_KEYS] * 2

    code, out, _err = cli(monkeypatch, db, "doc", "verify")
    assert code == 4
    failing = verified["data"]["documents"][1]
    assert out == (
        "doc verify failed: 1 of 2 documents\n"
        f"{failing['event_id']}\tmismatch\t{ITEM}\n"
    )


# ---- show and verify are reads; add needs the writer identity -----------------

NO_HARNESS = {"LEDGER_HARNESS": None}


def test_show_and_verify_are_reads_and_add_needs_the_harness_as_spec_set_does(
    db, tmp_path, monkeypatch
):
    assert doc_verb.VERB.writes is False
    code, added, err = add(monkeypatch, db, write(tmp_path / "a.md", b"read me"))
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    for argv in (
        ("doc", "show", ITEM),
        ("doc", "show", ITEM, "--id", event_id),
        ("doc", "show", ITEM, "--id", event_id, "--content"),
        ("doc", "verify"),
        ("doc", "verify", ITEM, "--id", event_id),
    ):
        code, out, err = invoke(monkeypatch, ["--json", "--db", db, *argv], env=NO_HARNESS)
        assert code == 0, (argv, err)
        assert json.loads(out)["ok"] is True, argv
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "doc", "show", ITEM, "--id", event_id, "--content"],
        env=NO_HARNESS,
    )
    assert (code, out) == (0, "read me"), err

    other = str(write(tmp_path / "b.md", b"never written"))
    for mode in (["--json"], []):
        refused_doc = invoke(
            monkeypatch,
            [*mode, "--db", db, "doc", "add", ITEM, other, "--role", "brief"],
            env=NO_HARNESS,
        )
        refused_spec = invoke(
            monkeypatch,
            [*mode, "--db", db, "spec", "set", ITEM, "--title", "T", "--body", "b"],
            env=NO_HARNESS,
        )
        assert refused_doc == refused_spec, mode
        assert refused_doc[0] == 3, mode
    code, out, _err = invoke(
        monkeypatch,
        ["--json", "--db", db, "doc", "add", ITEM, other, "--role", "brief"],
        env=NO_HARNESS,
    )
    assert (code, json.loads(out)["error"]["code"]) == (3, "missing_harness")
    assert len(doc_events(db)) == 1


# ---- symlinks: any symlink in FILE's path is refused, and never read ----------

SECRET = b"secret-token-0123456789abcdef"
# Documents are stored base64, so a secret read into the store would show
# up in that form, not as its raw bytes.
SECRET_B64 = base64.b64encode(SECRET)


def _store_bytes(db: str) -> bytes:
    """Every byte of the database and its sidecars."""
    home = Path(db).parent
    return b"".join(
        path.read_bytes() for path in sorted(home.iterdir())
        if path.name.startswith(Path(db).name) and path.is_file()
    )


def _store_holds_secret(db: str) -> bool:
    stored = _store_bytes(db)
    return SECRET in stored or SECRET_B64 in stored


def _output_holds_secret(*texts: str) -> bool:
    joined = "".join(texts)
    return SECRET.decode() in joined or SECRET_B64.decode() in joined


def test_the_secret_detector_sees_a_secret_that_was_added(db, tmp_path, monkeypatch):
    # The control for every "never read" assertion below: a secret that does
    # enter the store is found there, and in the --content output.
    code, added, err = add(monkeypatch, db, write(tmp_path / "plain.md", SECRET))
    assert code == 0, err
    assert _store_holds_secret(db)
    event_id = str(added["data"]["document"]["event_id"])
    code, shown, err = cli_json(
        monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content"
    )
    assert code == 0, err
    assert _output_holds_secret(json.dumps(shown))


def _assert_refused_unread(monkeypatch, db: str, file: Path | str) -> None:
    """``doc add FILE`` is refused ``doc_unreadable`` in JSON and text mode,
    the secret is in no output and nowhere in the store, and nothing is
    written."""
    code, refused, err = add(monkeypatch, db, file)
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), (file, refused, err)
    assert not _output_holds_secret(json.dumps(refused), err)
    code, out, err = cli(monkeypatch, db, "doc", "add", ITEM, str(file), "--role", "brief")
    assert code == 3, (file, out, err)
    assert out == ""
    assert not _output_holds_secret(out, err)
    assert doc_events(db) == []
    assert not _store_holds_secret(db)


def test_a_file_reached_through_a_symlinked_parent_is_refused_and_never_read(
    db, tmp_path, monkeypatch
):
    real = tmp_path / "elsewhere" / "real"
    real.mkdir(parents=True)
    write(real / "secret.md", SECRET)
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    through = tmp_path / "linked" / "secret.md"
    # The leaf is a regular file, so only the path check stands in the way.
    assert through.is_file() and not through.is_symlink()
    _assert_refused_unread(monkeypatch, db, through)
    code, refused, _err = add(monkeypatch, db, through)
    assert "goes through a symlink" in refused["error"]["message"]
    # Relative, from the working directory; and through the link and back
    # out with ``..``, which the OS resolves from the link's target
    # (elsewhere/beside.md), not lexically (beside.md here).
    monkeypatch.chdir(tmp_path)
    _assert_refused_unread(monkeypatch, db, "linked/secret.md")
    write(tmp_path / "elsewhere" / "beside.md", SECRET)
    assert (tmp_path / "linked" / ".." / "beside.md").read_bytes() == SECRET
    _assert_refused_unread(monkeypatch, db, "linked/../beside.md")
    code, refused, _err = add(monkeypatch, db, "linked/../beside.md")
    assert "goes through a symlink" in refused["error"]["message"]


def test_a_symlinked_leaf_is_refused_and_its_target_never_read(db, tmp_path, monkeypatch):
    secret = write(tmp_path / "credentials", SECRET)
    link = tmp_path / "innocent.md"
    link.symlink_to(secret)
    _assert_refused_unread(monkeypatch, db, link)
    # A dangling link is refused too, and a link to a link.
    dangling = tmp_path / "dangling.md"
    dangling.symlink_to(tmp_path / "nowhere")
    _assert_refused_unread(monkeypatch, db, dangling)
    chained = tmp_path / "chained.md"
    chained.symlink_to(link)
    _assert_refused_unread(monkeypatch, db, chained)


def _swap_to_symlink_after_the_path_check(monkeypatch, source: Path, target: Path) -> None:
    """Replace ``source`` with a symlink to ``target`` just after doc add's
    path check has seen a regular file there, as a racing writer could."""
    real_realpath = os.path.realpath

    def racing_realpath(path, *args, **kwargs):
        resolved = real_realpath(path, *args, **kwargs)
        if str(path) == str(source) and not source.is_symlink():
            source.unlink()
            source.symlink_to(target)
        return resolved

    monkeypatch.setattr(os.path, "realpath", racing_realpath)


def test_the_open_refuses_a_leaf_swapped_to_a_symlink_after_the_path_check(
    db, tmp_path, monkeypatch
):
    # Pins the open-time guard (O_NOFOLLOW): the path check passed on a
    # regular file, and the leaf is a symlink by the time it is opened.
    secret = write(tmp_path / "credentials", SECRET)
    source = write(tmp_path / "packet.md", b"what the check saw")
    _swap_to_symlink_after_the_path_check(monkeypatch, source, secret)
    code, refused, err = add(monkeypatch, db, source)
    assert source.is_symlink(), "the swap did not run"
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), err
    assert "not a readable regular file" in refused["error"]["message"]
    assert not _output_holds_secret(json.dumps(refused), err)
    assert doc_events(db) == []
    assert not _store_holds_secret(db)


def test_without_o_nofollow_the_lstat_check_refuses_a_symlink_before_any_open(
    db, tmp_path, monkeypatch
):
    # A platform with no O_NOFOLLOW takes the lstat path: a leaf that is a
    # symlink is refused there, and os.open is never called on it.
    secret = write(tmp_path / "credentials", SECRET)
    source = write(tmp_path / "packet.md", b"what the check saw")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    opened: list[str] = []
    real_open = os.open

    def recording_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    _swap_to_symlink_after_the_path_check(monkeypatch, source, secret)
    code, refused, err = add(monkeypatch, db, source)
    assert source.is_symlink(), "the swap did not run"
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), err
    assert str(source) not in opened, "os.open was called on a symlink"
    assert doc_events(db) == []
    assert not _store_holds_secret(db)

    link = tmp_path / "direct-link.md"
    link.symlink_to(secret)
    assert doc_verb._open_regular(str(link)) is None
    assert str(link) not in opened, "os.open was called on a symlink"


def test_without_o_nofollow_a_file_swapped_after_the_lstat_is_refused(
    db, tmp_path, monkeypatch
):
    # The lstat saw one regular file and the open reached another (a swap
    # between the two calls): the device and inode differ, so it is refused.
    source = write(tmp_path / "packet.txt", SECRET)
    decoy = write(tmp_path / "decoy", b"what the lstat saw")
    real_lstat = os.lstat
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(
        os, "lstat",
        lambda path, *a, **k: real_lstat(decoy) if os.fspath(path) == str(source)
        else real_lstat(path, *a, **k),
    )
    code, refused, err = add(monkeypatch, db, source)
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), err
    assert not _output_holds_secret(json.dumps(refused), err)
    assert doc_events(db) == []
    assert not _store_holds_secret(db)


# ---- FIFOs never block ----------------------------------------------------------

TIMEOUT = 20


def bounded_json(db: str, *argv: str) -> tuple[int, dict]:
    """The real CLI with --json in a child process; a hang past TIMEOUT fails
    the test instead of stalling the suite."""
    try:
        done = run_module(db, "--json", *argv, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.fail(f"ledger {' '.join(argv)} blocked for {TIMEOUT}s on a FIFO")
    return done.returncode, json.loads(done.stdout)


def test_a_fifo_given_to_doc_add_is_refused_without_blocking(db, tmp_path):
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    code, refused = bounded_json(db, "doc", "add", ITEM, str(fifo), "--role", "brief")
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), refused
    assert doc_events(db) == []


# ---- a forged inline document is never printed or reported ok -----------------


@pytest.mark.parametrize("forged", [{"sha256": "f" * 64}, {"size": 3}])
def test_show_content_refuses_a_forged_inline_document_and_prints_nothing(
    db, monkeypatch, forged
):
    _forge_inline(db, **forged)
    [event] = doc_events(db)
    event_id = str(event.id)
    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert (code, out) == (4, ""), "a forged inline document's bytes were printed"
    assert "inline bytes do not hash to the stored sha256; nothing printed" in err
    code, shown, _err = cli_json(
        monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content"
    )
    assert (code, shown["ok"], shown["error"]["code"], shown["data"]) == (
        4, False, "doc_mismatch", None
    )
    raw = run_module(db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert (raw.returncode, raw.stdout) == (4, b"")


@pytest.mark.parametrize("forged", [{"sha256": "f" * 64}, {"size": 3}])
def test_doc_verify_reports_a_forged_inline_document_as_mismatch(db, monkeypatch, forged):
    _forge_inline(db, **forged)
    code, verified, _err = cli_json(monkeypatch, db, "doc", "verify", ITEM)
    assert code == 4
    assert verified["error"]["code"] == "doc_verify_failed"
    assert [row["status"] for row in verified["data"]["documents"]] == ["mismatch"]


# ---- filenames are escaped in every refusal -------------------------------------

OSC52 = "\x1b]52;c;cHduZWQ=\x07"
OSC52_ESCAPED = "\\x1b]52;c;cHduZWQ=\\x07"


def _assert_escaped(out: str, err: str, fragment: str) -> None:
    assert "\x1b" not in out + err and "\x07" not in out + err
    assert fragment in err


def test_a_filename_holding_an_escape_sequence_is_escaped_in_the_refusal_text(
    db, tmp_path, monkeypatch
):
    gone = tmp_path / f"gone{OSC52}.md"
    code, out, err = cli(monkeypatch, db, "doc", "add", ITEM, str(gone), "--role", "brief")
    assert code == 3
    _assert_escaped(out, err, f"gone{OSC52_ESCAPED}.md")
    code, refused, _err = add(monkeypatch, db, gone)
    assert refused["error"]["code"] == "doc_unreadable"
    assert "\x1b" not in refused["error"]["message"]

    big = write(tmp_path / f"big{OSC52}.bin", b"x" * (DOC_INLINE_LIMIT + 1))
    code, out, err = cli(monkeypatch, db, "doc", "add", ITEM, str(big), "--role", "brief")
    assert code == 2
    _assert_escaped(out, err, f"big{OSC52_ESCAPED}.bin")

    target = write(tmp_path / "target.md", b"t")
    link = tmp_path / f"link{OSC52}.md"
    link.symlink_to(target)
    code, out, err = cli(monkeypatch, db, "doc", "add", ITEM, str(link), "--role", "brief")
    assert code == 3
    _assert_escaped(out, err, f"link{OSC52_ESCAPED}.md: its path goes through a symlink")
    assert doc_events(db) == []


def test_a_read_error_at_add_is_refused_with_the_filename_escaped(db, tmp_path, monkeypatch):
    source = write(tmp_path / f"reads{OSC52}.md", b"never stored")

    def failing_read(fd, size):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "read", failing_read)
    code, out, err = cli(monkeypatch, db, "doc", "add", ITEM, str(source), "--role", "brief")
    assert code == 3
    _assert_escaped(out, err, f"reads{OSC52_ESCAPED}.md: Input/output error")
    code, refused, _err = add(monkeypatch, db, source)
    assert refused["error"]["code"] == "doc_unreadable"
    assert refused["error"]["message"].endswith(f"reads{OSC52_ESCAPED}.md: Input/output error")
    monkeypatch.undo()
    assert doc_events(db) == []
