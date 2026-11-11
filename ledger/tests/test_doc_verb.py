"""Documents (LC5): ``doc.attached`` and ``ledger doc add|show|verify``.

A document is a content-addressed record on an item: the ledger computes its
sha256 and size from the bytes it read, and stores the bytes inline or the
path where they live. Nothing is printed as a document's content, or reported
``ok``, unless its bytes hash to the stored sha256. Only ``attach_document``
writes the kind; restore and replay recompute an inline document's digest
before inserting it.
"""
from __future__ import annotations

import base64
import dataclasses
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


def add(monkeypatch, db: str, file: Path, *extra: str, role: str = "brief"):
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


# ---- by path: verify ok, mismatch, missing ------------------------------------


def test_by_path_verify_ok_then_one_byte_changed_is_mismatch_then_missing(
    db, tmp_path, monkeypatch
):
    source = write(tmp_path / "packet.txt", b"the packet, version one\n")
    code, added, err = add(monkeypatch, db, source, "--by-path", role="packet")
    assert code == 0, err
    document = added["data"]["document"]
    assert document["storage"] == "path"
    assert document["path"] == str(source)
    event_id = str(document["event_id"])

    code, out, err = cli(monkeypatch, db, "doc", "verify", ITEM)
    assert (code, out) == (0, "ok documents=1\n"), err
    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert (code, out) == (0, "the packet, version one\n"), err

    changed = bytearray(source.read_bytes())
    changed[4] ^= 0x01  # one byte, same size
    source.write_bytes(bytes(changed))
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify", ITEM)
    assert code == 4, err
    assert [row["status"] for row in verified["data"]["documents"]] == ["mismatch"]
    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert code == 4
    assert out == "", "a mismatched document's bytes were printed"
    assert "does not hash to the stored sha256" in err
    code, shown, _err = cli_json(
        monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content"
    )
    assert (code, shown["ok"], shown["error"]["code"], shown["data"]) == (
        4, False, "doc_mismatch", None
    )

    source.unlink()
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify", ITEM)
    assert code == 4, err
    assert [row["status"] for row in verified["data"]["documents"]] == ["missing"]
    code, shown, _err = cli_json(
        monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content"
    )
    assert (code, shown["error"]["code"], shown["data"]) == (4, "doc_missing", None)


def test_a_path_that_is_a_directory_reads_as_missing(db, tmp_path, monkeypatch):
    source = write(tmp_path / "later-a-dir.txt", b"bytes")
    code, added, err = add(monkeypatch, db, source, "--by-path")
    assert code == 0, err
    source.unlink()
    source.mkdir()
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify")
    assert code == 4, err
    assert verified["data"]["documents"][0]["status"] == "missing"


def test_a_relative_path_is_stored_as_given_when_it_names_the_file_from_the_ledger_home(
    db, tmp_path, monkeypatch
):
    home = Path(db).parent
    write(home / "notes.txt", b"beside the database")
    monkeypatch.chdir(home)
    code, added, err = add(monkeypatch, db, Path("notes.txt"), "--by-path")
    assert code == 0, err
    assert added["data"]["document"]["path"] == "notes.txt"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write(elsewhere / "other.txt", b"not beside the database")
    monkeypatch.chdir(elsewhere)
    code, added, err = add(monkeypatch, db, Path("other.txt"), "--by-path")
    assert code == 0, err
    assert added["data"]["document"]["path"] == str(elsewhere / "other.txt")
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify", ITEM)
    assert code == 0, err
    assert [row["status"] for row in verified["data"]["documents"]] == ["ok", "ok"]


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


def test_inline_over_the_limit_refuses_and_writes_nothing(db, tmp_path, monkeypatch):
    big = write(tmp_path / "big.bin", b"x" * (DOC_INLINE_LIMIT + 1))
    code, refused, _err = add(monkeypatch, db, big, "--inline")
    assert code == 2
    assert refused["error"]["code"] == "usage"
    assert "over the 65536-byte inline limit" in refused["error"]["message"]
    assert doc_events(db) == []


def test_at_the_limit_is_inline_and_over_it_defaults_to_by_path(db, tmp_path, monkeypatch):
    exact = write(tmp_path / "exact.bin", b"y" * DOC_INLINE_LIMIT)
    code, added, err = add(monkeypatch, db, exact, "--inline")
    assert code == 0, err
    assert added["data"]["document"]["storage"] == "inline"
    big = write(tmp_path / "big.bin", b"z" * (DOC_INLINE_LIMIT + 1))
    code, added, err = add(monkeypatch, db, big)
    assert code == 0, err
    assert added["data"]["document"]["storage"] == "path"
    small = write(tmp_path / "small.bin", b"s")
    code, added, err = add(monkeypatch, db, small, "--by-path")
    assert code == 0, err
    assert added["data"]["document"]["storage"] == "path"


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
        ("doc", "add", ITEM, str(source), "--role", "brief", "--inline", "--by-path"),
        ("doc", "add", ITEM, str(source), "--role", "nonsense"),
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


def test_the_schema_takes_exactly_one_of_inline_bytes_and_path():
    base = {"role": "brief", "sha256": "a" * 64, "size": 0, "mime": "text/plain"}
    validate_payload(DOC_KIND, {**base, "content_b64": ""})
    validate_payload(DOC_KIND, {**base, "path": "/tmp/x"})
    for payload in (base, {**base, "content_b64": "", "path": "/tmp/x"}):
        with pytest.raises(ValidationError, match="exactly-one-of:content_b64,path"):
            validate_payload(DOC_KIND, payload)


# ---- the chain check -----------------------------------------------------------


def test_ledger_verify_passes_on_a_ledger_holding_documents(db, tmp_path, monkeypatch):
    assert add(monkeypatch, db, write(tmp_path / "a.md", b"inline"))[0] == 0
    assert add(monkeypatch, db, write(tmp_path / "b.txt", b"by path"), "--by-path")[0] == 0
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


def test_a_correct_inline_and_a_path_document_restore(db, tmp_path, monkeypatch):
    path_source = write(tmp_path / "kept.txt", b"kept by path")
    assert add(monkeypatch, db, write(tmp_path / "in.md", b"kept inline"))[0] == 0
    assert add(monkeypatch, db, path_source, "--by-path", role="packet")[0] == 0
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


def test_show_content_of_a_path_document_is_binary_safe(db, tmp_path, monkeypatch):
    source = write(tmp_path / "binary.bin", BINARY)
    code, added, err = add(monkeypatch, db, source, "--by-path")
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    shown = run_module(db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout == BINARY


def test_the_show_and_verify_json_shapes_are_pinned(db, tmp_path, monkeypatch):
    assert add(monkeypatch, db, write(tmp_path / "a.md", b"a"))[0] == 0
    code, shown, err = cli_json(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    assert set(shown) == ENVELOPE_KEYS
    assert set(shown["data"]) == {"item_id", "documents"}
    assert [set(row) for row in shown["data"]["documents"]] == [ROW_KEYS]
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify")
    assert code == 0, err
    assert verified["ok"] is True and verified["error"] is None
    assert set(verified["data"]) == {"documents"}
    assert [set(row) for row in verified["data"]["documents"]] == [VERIFY_ROW_KEYS]


def test_a_failed_doc_verify_reads_ok_false_exit_4_with_every_row(db, tmp_path, monkeypatch):
    kept = write(tmp_path / "kept.txt", b"unchanged")
    changed = write(tmp_path / "changed.txt", b"will change")
    assert add(monkeypatch, db, kept, "--by-path", role="brief")[0] == 0
    assert add(monkeypatch, db, changed, "--by-path", role="retro")[0] == 0
    changed.write_bytes(b"has changed")
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

    changed.write_bytes(b"will change")
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify")
    assert code == 0, err
    assert verified["ok"] is True and verified["error"] is None


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


# ---- symlinks are never followed ----------------------------------------------

SECRET = b"secret-token-0123456789abcdef"


def _store_bytes(db: str) -> bytes:
    """Every byte of the database and its sidecars."""
    home = Path(db).parent
    return b"".join(
        path.read_bytes() for path in sorted(home.iterdir())
        if path.name.startswith(Path(db).name) and path.is_file()
    )


def _assert_reads_as_missing(monkeypatch, db: str, event_id: str, forbidden: bytes) -> None:
    """verify says ``missing`` and ``show --content`` refuses ``doc_missing``,
    and ``forbidden`` is printed nowhere."""
    code, verified, err = cli_json(monkeypatch, db, "doc", "verify", ITEM)
    assert code == 4, err
    assert [row["status"] for row in verified["data"]["documents"]] == ["missing"]
    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert (code, out) == (4, ""), "a document read through a symlink was printed"
    assert forbidden.decode() not in out + err
    code, shown, _err = cli_json(
        monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content"
    )
    assert (code, shown["error"]["code"], shown["data"]) == (4, "doc_missing", None)
    assert forbidden.decode() not in json.dumps(shown)


def test_a_symlink_given_to_doc_add_is_refused_and_its_target_never_read(
    db, tmp_path, monkeypatch
):
    secret = write(tmp_path / "credentials", SECRET)
    link = tmp_path / "innocent.md"
    link.symlink_to(secret)
    for extra in ((), ("--inline",), ("--by-path",)):
        code, refused, err = add(monkeypatch, db, link, *extra)
        assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), extra
        assert SECRET.decode() not in json.dumps(refused) + err
    assert doc_events(db) == []
    assert SECRET not in _store_bytes(db)


@pytest.mark.parametrize("target", ["same-bytes", "secret"])
def test_a_symlink_put_at_a_stored_path_reads_as_missing_and_is_never_printed(
    db, tmp_path, monkeypatch, target
):
    body = b"the packet, by path"
    source = write(tmp_path / "packet.txt", body)
    code, added, err = add(monkeypatch, db, source, "--by-path", role="packet")
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    # The same bytes elsewhere would verify ok if the link were followed.
    pointed = write(tmp_path / "pointed", body if target == "same-bytes" else SECRET)
    source.unlink()
    source.symlink_to(pointed)
    _assert_reads_as_missing(monkeypatch, db, event_id, forbidden=pointed.read_bytes())
    assert SECRET not in _store_bytes(db)


def test_without_o_nofollow_the_lstat_check_still_refuses_a_symlink(
    db, tmp_path, monkeypatch
):
    body = b"the packet, by path"
    source = write(tmp_path / "packet.txt", body)
    code, added, err = add(monkeypatch, db, source, "--by-path", role="packet")
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    secret = write(tmp_path / "credentials", SECRET)
    link = tmp_path / "innocent.md"
    link.symlink_to(secret)
    pointed = write(tmp_path / "pointed", body)
    source.unlink()
    source.symlink_to(pointed)
    # A platform with no O_NOFOLLOW takes the lstat path.
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    code, refused, err = add(monkeypatch, db, link)
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), err
    _assert_reads_as_missing(monkeypatch, db, event_id, forbidden=body)
    assert SECRET not in _store_bytes(db)


def test_without_o_nofollow_a_file_swapped_after_the_lstat_is_refused(
    db, tmp_path, monkeypatch
):
    # The lstat saw one regular file and the open reached another (a swap
    # between the two calls): the device and inode differ, so it is refused.
    body = b"the packet, by path"
    source = write(tmp_path / "packet.txt", body)
    code, added, err = add(monkeypatch, db, source, "--by-path", role="packet")
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    decoy = write(tmp_path / "decoy", b"what the lstat saw")
    real_lstat = os.lstat
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(
        os, "lstat",
        lambda path, *a, **k: real_lstat(decoy) if str(path) == str(source)
        else real_lstat(path, *a, **k),
    )
    _assert_reads_as_missing(monkeypatch, db, event_id, forbidden=body)


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


def test_a_fifo_at_a_stored_path_reads_as_missing_without_blocking(db, tmp_path, monkeypatch):
    source = write(tmp_path / "packet.txt", b"the packet")
    code, added, err = add(monkeypatch, db, source, "--by-path", role="packet")
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    source.unlink()
    os.mkfifo(source)
    code, verified = bounded_json(db, "doc", "verify", ITEM)
    assert code == 4, verified
    assert [row["status"] for row in verified["data"]["documents"]] == ["missing"]
    code, shown = bounded_json(db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert (code, shown["error"]["code"], shown["data"]) == (4, "doc_missing", None)


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
    assert "does not hash to the stored sha256" in err
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


# ---- a control character in a stored path ---------------------------------------


def test_a_control_character_in_a_stored_path_is_refused_at_add_and_on_restore(
    db, tmp_path, monkeypatch
):
    evil = write(tmp_path / "evil\x1b[2Jname.txt", b"bytes")
    code, refused, _err = add(monkeypatch, db, evil, "--by-path")
    assert code == 3
    assert refused["error"]["message"] == "ask_control_character:path"
    assert doc_events(db) == []

    clean = write(tmp_path / "clean.txt", b"clean")
    assert add(monkeypatch, db, clean, "--by-path")[0] == 0
    export = tmp_path / "export"
    with open_ledger(db, create=False) as lg:
        export_events(lg, export)
        events = lg.events()
    document = events[-1]
    assert document.kind == DOC_KIND
    # The last event of a valid export, its path rewritten and its hash
    # recomputed, so the chain still verifies.
    forged = dataclasses.replace(
        document, payload={**document.payload, "path": str(tmp_path / "evil\x1bname.txt")}
    )
    forged = dataclasses.replace(forged, hash=event_digest(hash_body(forged)))
    (export / event_filename(forged.id)).write_text(
        canonical_dumps(event_record(forged)) + "\n", encoding="utf-8"
    )
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises((ValidationError, LedgerError), match="ask_control_character:path"):
        restore_export(export, dest, system="home")
    assert not any(dest.parent.glob("ledger.db*")), "a refused restore left files behind"
    replaced = [forged if event.id == forged.id else event for event in events]
    with open_ledger(tmp_path / "fresh.db") as fresh:
        with pytest.raises(
            LedgerError,
            match=r"restore refuses event \d+ \(doc\.attached\): ask_control_character:path",
        ):
            replay_events(fresh, replaced)
        assert fresh.events() == []


# ---- filenames and stored paths are escaped in refusals and text --------------

OSC52 = "\x1b]52;c;cHduZWQ=\x07"
OSC52_ESCAPED = "\\x1b]52;c;cHduZWQ=\\x07"


def test_a_filename_holding_an_escape_sequence_is_escaped_in_the_refusal_text(
    db, tmp_path, monkeypatch
):
    gone = tmp_path / f"gone{OSC52}.md"
    code, out, err = cli(monkeypatch, db, "doc", "add", ITEM, str(gone), "--role", "brief")
    assert code == 3
    assert "\x1b" not in out + err and "\x07" not in out + err
    assert f"gone{OSC52_ESCAPED}.md" in err
    code, refused, _err = add(monkeypatch, db, gone)
    assert refused["error"]["code"] == "doc_unreadable"
    assert "\x1b" not in refused["error"]["message"]

    big = write(tmp_path / f"big{OSC52}.bin", b"x" * (DOC_INLINE_LIMIT + 1))
    code, out, err = cli(
        monkeypatch, db, "doc", "add", ITEM, str(big), "--role", "brief", "--inline"
    )
    assert code == 2
    assert "\x1b" not in out + err and "\x07" not in out + err
    assert f"big{OSC52_ESCAPED}.bin" in err
    assert doc_events(db) == []


def test_a_stored_path_is_escaped_in_show_text_and_in_a_refusal(db, tmp_path, monkeypatch):
    # U+2028 is not a control character, so the schema stores it; the text
    # output must still never print it raw.
    odd = write(tmp_path / "line break.txt", b"odd")
    code, added, err = add(monkeypatch, db, odd, "--by-path")
    assert code == 0, err
    event_id = str(added["data"]["document"]["event_id"])
    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    assert " " not in out
    assert "line\\u2028break.txt" in out
    odd.unlink()
    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content")
    assert (code, out) == (4, "")
    assert " " not in err
    assert "line\\u2028break.txt" in err
