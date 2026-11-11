"""Documents (LC5): ``doc.attached`` and ``ledger doc add|show|verify``.

A document is a content-addressed record on an item: the ledger computes its
sha256 and size from the bytes it read and stores the bytes inline. Documents
are inline-only, so no read after ``doc add`` touches the filesystem, and
``doc add`` refuses a file whose path holds a symlink anywhere; a file with a
second hard link is a regular file and is attached. Nothing is printed as a
document's content, or reported ``ok``, unless its stored bytes hash to the
stored sha256. Only ``attach_document`` writes the kind; restore and replay
recompute the digest before inserting it and refuse a ``path``. Bytes holding
a credential shape are refused at add unless the override flag marks them,
and an unmarked one is refused at restore, at replay and at export.
"""
from __future__ import annotations

import base64
import dataclasses
import errno
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.export import (
    ExportDocCredential,
    event_filename,
    event_record,
    export_events,
    hash_body,
    load_export,
    read_watermark,
    write_event_files,
    write_stream_digest,
    write_watermark,
)
from ledger.restore import replay_events, restore_export
from ledger.schema import DOC_INLINE_LIMIT, DOC_KIND, ValidationError, validate_payload
from ledger.store import LedgerError, canonical_dumps, event_digest, open_ledger
from ledger.verbs import CliError
from ledger.verbs import doc as doc_verb
from ledger.verbs import export as export_verb

from test_cli import invoke, seed
from test_credscan import NEAR_MISSES, SAMPLES, wrap

ITEM = "it-1"
PKG_PARENT = Path(__file__).resolve().parent.parent.parent
BINARY = b"\x00head\xff\x00\r\n\xfe\xff tail\x00"
ROW_KEYS = {
    "event_id", "item_id", "role", "title", "mime", "size", "sha256", "storage", "path",
    "credential_override",
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
    db: str, *argv: str, timeout: float | None = None, stdin: bytes | None = None
) -> subprocess.CompletedProcess:
    """The real CLI in a child process, stdout captured as bytes."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.pop("PYTHONPATH", None)
    env["LEDGER_HARNESS"] = "pytest"
    return subprocess.run(
        [sys.executable, "-m", "ledger", "--db", db, *argv],
        cwd=PKG_PARENT, capture_output=True, env=env, check=False, timeout=timeout,
        input=stdin if stdin is not None else b"",
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
    assert added["data"]["credential_override"] is False
    assert document["credential_override"] is False
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


# ---- restore and replay refuse a document for an item that does not exist -----


MISSING_ITEM = "it-missing"


def _attach_to_a_missing_item(db: str) -> int:
    """Write a well-formed inline document on an item with no ``item.created``
    (a store a hand-edited writer left behind), through the writer's own
    capability, which leaves the item check to ``doc add``. Returns its id."""
    body = b"orphan bytes"
    payload = {
        "role": "brief",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "mime": "text/plain",
        "content_b64": base64.b64encode(body).decode("ascii"),
    }
    with open_ledger(db, create=False) as lg:
        event = lg._append_doc_event(
            doc_verb._DOC_WRITER_CAPABILITY, kind=DOC_KIND, payload=payload,
            item_id=MISSING_ITEM, idempotency_key="orphan",
        )
    return event.id


def _item_missing(event_id: int) -> str:
    return re.escape(
        f"restore refuses event {event_id} (doc.attached): "
        f"restore_doc_item_missing: item not found: {MISSING_ITEM}"
    )


def test_restore_refuses_a_document_for_an_item_that_does_not_exist(db, tmp_path):
    event_id = _attach_to_a_missing_item(db)
    with open_ledger(db, create=False) as lg:
        export_events(lg, tmp_path / "export")
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises(LedgerError, match=_item_missing(event_id)):
        restore_export(tmp_path / "export", dest, system="home")
    assert not any(dest.parent.glob("ledger.db*")), "a refused restore left files behind"


def test_replay_events_refuses_a_document_for_an_item_that_does_not_exist(db, tmp_path):
    event_id = _attach_to_a_missing_item(db)
    with open_ledger(db, create=False) as lg:
        events = lg.events()
    # The seed's own rows come first and pass, so the refusal rolls them back.
    assert events[-1].id == event_id and len(events) > 1
    with open_ledger(tmp_path / "fresh.db") as fresh:
        with pytest.raises(LedgerError, match=_item_missing(event_id)):
            replay_events(fresh, events)
        assert fresh.events() == [], "a refused replay inserted events"


def test_a_document_whose_item_exists_restores_and_replays(db, tmp_path, monkeypatch):
    assert add(monkeypatch, db, write(tmp_path / "in.md", b"on a real item"))[0] == 0
    with open_ledger(db, create=False) as lg:
        events = lg.events()
        export_events(lg, tmp_path / "export")
    [created] = [e for e in events if e.kind == "item.created" and e.item_id == ITEM]
    [document] = [e for e in events if e.kind == DOC_KIND]
    assert created.id < document.id
    # The item is created earlier in the same batch: restore and replay insert it.
    dest = tmp_path / "restored" / "ledger.db"
    restore_export(tmp_path / "export", dest, system="home")
    assert [e.payload for e in doc_events(str(dest))] == [document.payload]
    with open_ledger(tmp_path / "fresh.db") as fresh:
        replay_events(fresh, events)
        assert [e.id for e in fresh.events(kind=DOC_KIND)] == [document.id]
    # The item is already in the destination: a later batch holding only the
    # document replays too.
    split = events.index(document)
    with open_ledger(tmp_path / "split.db") as split_db:
        replay_events(split_db, events[:split])
        replay_events(split_db, events[split:])
        assert [e.id for e in split_db.events(kind=DOC_KIND)] == [document.id]


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
    # Relative, from the working directory. A path through the link and
    # back out with ``..`` is refused for its ``..`` (see below).
    monkeypatch.chdir(tmp_path)
    _assert_refused_unread(monkeypatch, db, "linked/secret.md")


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


def test_a_file_with_a_second_hard_link_is_attached_and_its_bytes_stored(
    db, tmp_path, monkeypatch
):
    # The boundary: doc add does not vouch for where bytes came from. A hard
    # link is a second name for a regular file, not a symlink, so the walk
    # reads it like any other file, by either name.
    original = write(tmp_path / "original.md", SECRET)
    linked = tmp_path / "linked.md"
    os.link(original, linked)
    assert os.stat(linked).st_nlink == 2 and not linked.is_symlink()
    code, added, err = add(monkeypatch, db, linked)
    assert code == 0, err
    document = added["data"]["document"]
    assert document["sha256"] == hashlib.sha256(SECRET).hexdigest()
    assert document["size"] == len(SECRET)
    code, again, err = add(monkeypatch, db, original)
    assert code == 0, err
    assert again["data"]["unchanged"] is True
    assert again["data"]["document"]["event_id"] == document["event_id"]
    [stored] = doc_events(db)
    assert base64.b64decode(stored.payload["content_b64"]) == SECRET


# ---- the no-follow walk: one component at a time, from a descriptor -------------


def _patch_open(monkeypatch, wrapper) -> None:
    """Install ``wrapper`` as ``os.open``, declared descriptor-relative as the
    real one is, so the walk's platform check still sees dir_fd support."""
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {wrapper})
    monkeypatch.setattr(os, "open", wrapper)


def _record_opens(monkeypatch) -> list[str]:
    """Every name ``os.open`` is called with, in order."""
    opened: list[str] = []
    real_open = os.open

    def recording_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    _patch_open(monkeypatch, recording_open)
    return opened


def _swap_after_opening(monkeypatch, component: str, swap) -> list[str]:
    """Run ``swap()`` once, just after the walk has opened ``component``, as
    a racing writer could between two component opens. The returned list
    names the component once the swap has run."""
    ran: list[str] = []
    real_open = os.open

    def racing_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if not ran and os.fspath(path) == component:
            swap()
            ran.append(component)
        return fd

    _patch_open(monkeypatch, racing_open)
    return ran


def _parent_swap_tree(tmp_path: Path):
    """``walk-top/walk-parent/packet.md`` and a ``swap`` that moves
    ``walk-parent`` aside and puts a symlink to a directory holding a
    secret ``packet.md`` in its place."""
    top = tmp_path / "walk-top"
    parent = top / "walk-parent"
    parent.mkdir(parents=True)
    source = write(parent / "packet.md", b"what the walk started on")
    other = tmp_path / "walk-other"
    other.mkdir()
    write(other / "packet.md", SECRET)

    def swap() -> None:
        parent.rename(top / "walk-parent-moved")
        parent.symlink_to(other, target_is_directory=True)

    return source, parent, swap


def test_a_parent_swapped_for_a_symlink_before_the_walk_opens_it_is_refused(
    db, tmp_path, monkeypatch
):
    # The walk has opened the grandparent; the parent becomes a symlink to
    # another directory before the walk opens it. The no-follow open of the
    # parent fails, so the other directory's file is never read.
    source, parent, swap = _parent_swap_tree(tmp_path)
    ran = _swap_after_opening(monkeypatch, "walk-top", swap)
    code, refused, err = add(monkeypatch, db, source)
    assert ran == ["walk-top"] and parent.is_symlink(), "the swap did not run"
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), (refused, err)
    assert "goes through a symlink" in refused["error"]["message"]
    assert not _output_holds_secret(json.dumps(refused), err)
    assert doc_events(db) == []
    assert not _store_holds_secret(db)


def test_a_parent_swapped_after_the_walk_opened_it_still_reads_the_opened_directory(
    db, tmp_path, monkeypatch
):
    # The walk has opened the parent; it then becomes a symlink to another
    # directory. The leaf is opened relative to the parent's descriptor, so
    # it is the file in the directory the walk opened, never the other one.
    source, parent, swap = _parent_swap_tree(tmp_path)
    ran = _swap_after_opening(monkeypatch, "walk-parent", swap)
    code, added, err = add(monkeypatch, db, source)
    assert ran == ["walk-parent"] and parent.is_symlink(), "the swap did not run"
    assert code == 0, (added, err)
    [stored] = doc_events(db)
    assert base64.b64decode(stored.payload["content_b64"]) == b"what the walk started on"
    assert not _output_holds_secret(json.dumps(added), err)
    assert not _store_holds_secret(db)


def test_a_leaf_swapped_to_a_symlink_after_its_parent_opened_is_refused(
    db, tmp_path, monkeypatch
):
    secret = write(tmp_path / "credentials", SECRET)
    parent = tmp_path / "walk-parent"
    parent.mkdir()
    source = write(parent / "packet.md", b"what the walk started on")

    def swap() -> None:
        source.unlink()
        source.symlink_to(secret)

    ran = _swap_after_opening(monkeypatch, "walk-parent", swap)
    code, refused, err = add(monkeypatch, db, source)
    assert ran == ["walk-parent"] and source.is_symlink(), "the swap did not run"
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), (refused, err)
    assert "goes through a symlink" in refused["error"]["message"]
    assert not _output_holds_secret(json.dumps(refused), err)
    assert doc_events(db) == []
    assert not _store_holds_secret(db)


def test_dotdot_after_a_symlinked_directory_is_refused_before_any_component_opens(
    db, tmp_path, monkeypatch
):
    # ``link/../secret.md``: lexically the sibling of ``link``; the OS would
    # resolve ``..`` from the link's target. Neither is read: ``..`` is
    # refused as given, before the walk opens a component.
    real = tmp_path / "elsewhere" / "real"
    real.mkdir(parents=True)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    write(tmp_path / "secret.md", SECRET)
    write(tmp_path / "elsewhere" / "secret.md", SECRET)
    monkeypatch.chdir(tmp_path)
    opened = _record_opens(monkeypatch)
    for given in ("link/../secret.md", f"{tmp_path}/link/../secret.md"):
        _assert_refused_unread(monkeypatch, db, given)
        code, refused, _err = add(monkeypatch, db, given)
        assert "its path holds a '..' component" in refused["error"]["message"], given
    assert "link" not in opened, "the walk opened a component of a path holding '..'"


@pytest.mark.parametrize(
    "given", ["sub/../a.md", "./sub/../a.md", "sub/..", "../a.md", "{tmp}/sub/../a.md"]
)
def test_dotdot_anywhere_is_refused_even_when_it_resolves_inside_the_tree(
    db, tmp_path, monkeypatch, given
):
    (tmp_path / "sub").mkdir()
    write(tmp_path / "a.md", b"inside the tree")
    monkeypatch.chdir(tmp_path)
    given = given.format(tmp=tmp_path)
    code, refused, err = add(monkeypatch, db, given)
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), (refused, err)
    assert "its path holds a '..' component" in refused["error"]["message"]
    assert doc_events(db) == []


@pytest.mark.parametrize("given", ["{file}/", "{file}//", "sub/", ".", "sub/.", "/", ""])
def test_a_file_ending_in_slash_or_dot_or_empty_is_refused(db, tmp_path, monkeypatch, given):
    (tmp_path / "sub").mkdir()
    regular = write(tmp_path / "a.md", b"a regular file")
    monkeypatch.chdir(tmp_path)
    given = given.format(file=regular)
    code, refused, err = add(monkeypatch, db, given)
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), (given, refused, err)
    expected = "it is empty" if given == "" else "it names a directory, not a file"
    assert expected in refused["error"]["message"], given
    assert doc_events(db) == []


def test_repeated_slashes_and_dot_components_are_skipped(db, tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    cases = {
        "/" + str(write(tmp_path / "abs.md", b"double-slash absolute")): b"double-slash absolute",
        f"{tmp_path}//sub//mid.md": b"double-slash inside",
        "sub//rel.md": b"double-slash relative",
        "./dot.md": b"leading dot",
        "././sub/./deep.md": b"dots inside",
    }
    write(tmp_path / "sub" / "mid.md", b"double-slash inside")
    write(tmp_path / "sub" / "rel.md", b"double-slash relative")
    write(tmp_path / "dot.md", b"leading dot")
    write(tmp_path / "sub" / "deep.md", b"dots inside")
    for given, body in cases.items():
        code, added, err = add(monkeypatch, db, given)
        assert code == 0, (given, added, err)
        assert added["data"]["unchanged"] is False, given
        event_id = str(added["data"]["document"]["event_id"])
        code, shown, err = cli_json(
            monkeypatch, db, "doc", "show", ITEM, "--id", event_id, "--content"
        )
        assert base64.b64decode(shown["data"]["document"]["content_b64"]) == body, given
    assert len(doc_events(db)) == len(cases)


@pytest.mark.parametrize("missing", ["O_NOFOLLOW", "O_DIRECTORY", "dir_fd"])
def test_a_platform_without_no_follow_opens_refuses_before_opening_the_leaf(
    db, tmp_path, monkeypatch, missing
):
    source = write(tmp_path / "packet.md", b"never opened")
    opened = _record_opens(monkeypatch)
    if missing == "dir_fd":
        monkeypatch.setattr(os, "supports_dir_fd", set())
    else:
        monkeypatch.delattr(os, missing)
    code, refused, err = add(monkeypatch, db, source)
    assert (code, refused["error"]["code"]) == (3, "doc_unreadable"), (refused, err)
    assert "cannot open a path without following symlinks" in refused["error"]["message"]
    assert "packet.md" not in opened, "os.open was called on the leaf"
    assert doc_events(db) == []


def _open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def test_the_walk_leaks_no_descriptor_accepted_or_refused(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    source = write(nested / "doc.md", b"counted")
    (tmp_path / "a" / "link").symlink_to(nested, target_is_directory=True)
    (tmp_path / "a" / "leaf-link.md").symlink_to(source)
    monkeypatch.chdir(tmp_path)
    before = _open_descriptors()
    for accepted in (str(source), "a/b/doc.md"):
        assert doc_verb._read_input(accepted) == b"counted"
        assert _open_descriptors() == before, accepted
    for refused in (
        str(tmp_path / "a" / "link" / "doc.md"),
        "a/link/doc.md",
        str(tmp_path / "a" / "leaf-link.md"),
        str(tmp_path / "a" / "missing" / "doc.md"),
        str(nested),
        str(source) + "/",
        "a/../a/b/doc.md",
    ):
        with pytest.raises(CliError) as caught:
            doc_verb._read_input(refused)
        assert caught.value.code == "doc_unreadable", refused
        assert _open_descriptors() == before, refused


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


# ---- standard input: a single hyphen ---------------------------------------------


def _stdin(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))


def test_a_single_hyphen_reads_standard_input_and_dot_slash_hyphen_the_file(
    db, tmp_path, monkeypatch
):
    # A file named "-" sits in the working directory: "-" still reads
    # standard input, and "./-" reads that file.
    monkeypatch.chdir(tmp_path)
    named = b"the file whose name is a hyphen"
    write(tmp_path / "-", named)
    _stdin(monkeypatch, BINARY)
    code, added, err = add(monkeypatch, db, "-")
    assert code == 0, err
    document = added["data"]["document"]
    assert document["sha256"] == hashlib.sha256(BINARY).hexdigest()
    assert document["size"] == len(BINARY)
    assert document["mime"] == "application/octet-stream"
    code, added, err = add(monkeypatch, db, "./-")
    assert code == 0, err
    assert added["data"]["document"]["sha256"] == hashlib.sha256(named).hexdigest()
    assert [base64.b64decode(e.payload["content_b64"]) for e in doc_events(db)] == [
        BINARY, named,
    ]


def test_standard_input_through_the_real_cli(db):
    done = run_module(
        db, "--json", "doc", "add", ITEM, "-", "--role", "packet", stdin=BINARY
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["data"]["document"]["size"] == len(BINARY)
    [stored] = doc_events(db)
    assert base64.b64decode(stored.payload["content_b64"]) == BINARY


def test_standard_input_over_the_limit_is_a_usage_error(db, monkeypatch):
    _stdin(monkeypatch, b"x" * (DOC_INLINE_LIMIT + 1))
    code, refused, err = add(monkeypatch, db, "-")
    assert (code, refused["error"]["code"]) == (2, "usage"), (refused, err)
    assert refused["error"]["message"].startswith(
        "standard input is over the 65536-byte inline limit"
    )
    assert doc_events(db) == []
    _stdin(monkeypatch, b"x" * DOC_INLINE_LIMIT)
    code, added, err = add(monkeypatch, db, "-")
    assert code == 0, err
    assert added["data"]["document"]["size"] == DOC_INLINE_LIMIT


# ---- credential shapes at add ---------------------------------------------------

SHAPES = sorted(SAMPLES)


def _assert_no_sample(sample: bytes, *texts: str) -> None:
    """None of ``texts`` holds the matched bytes, raw or base64."""
    joined = "".join(texts)
    assert sample.decode() not in joined
    assert base64.b64encode(sample).decode() not in joined


def _refusal(name: str, shape: str, offset: int) -> str:
    return (
        f"{name} holds a credential shape ({shape} at byte {offset}); nothing was "
        "written. Pass --allow-credential-shapes to attach it anyway"
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_a_credential_shape_is_refused_at_add_and_nothing_is_written(
    db, tmp_path, monkeypatch, shape
):
    sample = SAMPLES[shape]
    body = wrap(sample)
    offset = body.index(sample)
    source = write(tmp_path / "notes.md", body)
    code, refused, err = add(monkeypatch, db, source)
    assert (code, refused["ok"]) == (3, False), f"a {shape} document was added: {refused}"
    assert refused["error"]["code"] == "doc_credential_shape"
    assert refused["error"]["message"] == _refusal(str(source), shape, offset)
    _assert_no_sample(sample, json.dumps(refused), err)
    code, out, err = cli(monkeypatch, db, "doc", "add", ITEM, str(source), "--role", "brief")
    assert (code, out) == (3, "")
    assert _refusal(str(source), shape, offset) in err
    _assert_no_sample(sample, out, err)
    assert doc_events(db) == []


@pytest.mark.parametrize("shape", SHAPES)
def test_a_credential_shape_attached_with_the_flag_carries_the_marker(
    db, tmp_path, monkeypatch, shape
):
    source = write(tmp_path / "notes.md", wrap(SAMPLES[shape]))
    code, added, err = add(monkeypatch, db, source, "--allow-credential-shapes")
    assert code == 0, err
    assert added["data"]["credential_override"] is True
    assert added["data"]["document"]["credential_override"] is True
    [stored] = doc_events(db)
    assert stored.payload["credential_override"] is True
    assert set(stored.payload) == PAYLOAD_KEYS | {"credential_override"}
    code, shown, err = cli_json(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    assert [row["credential_override"] for row in shown["data"]["documents"]] == [True]
    code, out, err = cli(monkeypatch, db, "doc", "show", ITEM)
    assert code == 0, err
    assert out.endswith("\tcredential-override\n")
    _assert_no_sample(SAMPLES[shape], out, err)


def test_an_overridden_add_repeated_without_the_flag_is_refused_not_unchanged(
    db, tmp_path, monkeypatch
):
    source = write(tmp_path / "key.txt", wrap(SAMPLES["private-key"]))
    code, out, err = cli(
        monkeypatch, db, "doc", "add", ITEM, str(source), "--role", "brief",
        "--allow-credential-shapes",
    )
    assert code == 0, err
    assert out.endswith("\tcredential-override\n")
    code, refused, err = add(monkeypatch, db, source)
    assert (code, refused["ok"]) == (3, False), f"reported unchanged: {refused}"
    assert refused["error"]["code"] == "doc_credential_shape"
    code, again, err = add(monkeypatch, db, source, "--allow-credential-shapes")
    assert code == 0, err
    assert again["data"]["unchanged"] is True
    assert again["data"]["credential_override"] is True
    assert len(doc_events(db)) == 1


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_near_misses_are_attached_with_no_marker(db, tmp_path, monkeypatch, name):
    code, added, err = add(monkeypatch, db, write(tmp_path / "near.md", wrap(NEAR_MISSES[name])))
    assert code == 0, (name, err)
    assert added["data"]["credential_override"] is False
    [stored] = doc_events(db)
    assert set(stored.payload) == PAYLOAD_KEYS


def test_the_flag_on_bytes_with_no_shape_writes_no_marker(db, tmp_path, monkeypatch):
    code, added, err = add(
        monkeypatch, db, write(tmp_path / "clean.md", b"nothing to hide"),
        "--allow-credential-shapes",
    )
    assert code == 0, err
    assert added["data"]["credential_override"] is False
    [stored] = doc_events(db)
    assert set(stored.payload) == PAYLOAD_KEYS


def test_a_credential_shape_on_standard_input_is_refused_naming_standard_input(
    db, monkeypatch
):
    sample = SAMPLES["slack-token"]
    _stdin(monkeypatch, sample)
    code, refused, err = add(monkeypatch, db, "-")
    assert (code, refused["error"]["code"]) == (3, "doc_credential_shape"), (refused, err)
    assert refused["error"]["message"] == _refusal("standard input", "slack-token", 0)
    _assert_no_sample(sample, json.dumps(refused), err)
    assert doc_events(db) == []


def test_the_flag_belongs_to_add_only(db, monkeypatch):
    for argv in (
        ("doc", "show", ITEM, "--allow-credential-shapes"),
        ("doc", "verify", "--allow-credential-shapes"),
    ):
        code, refused, _err = cli_json(monkeypatch, db, *argv)
        assert (code, refused["error"]["code"]) == (2, "usage"), argv
        assert "--allow-credential-shapes" in refused["error"]["message"], argv


def test_the_schema_takes_credential_override_only_as_true():
    base = {"role": "brief", "sha256": "a" * 64, "size": 0, "mime": "text/plain",
            "content_b64": ""}
    validate_payload(DOC_KIND, {**base, "credential_override": True})
    for value in (False, 1, 0, "true", None, [True]):
        with pytest.raises(ValidationError, match="bad-type:credential_override"):
            validate_payload(DOC_KIND, {**base, "credential_override": value})


# ---- credential shapes at restore and replay ---------------------------------------


def _store_unmarked(db: str, sample: bytes, *, content_b64: str | None = None) -> int:
    """Store an inline document holding ``sample`` with no marker, through
    the writer's own capability: the add-time scan is bypassed, as a store
    an older ledger or a hand-edited writer left behind. Returns its id."""
    body = wrap(sample)
    payload = {
        "role": "brief",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "mime": "text/plain",
        "content_b64": (
            content_b64 if content_b64 is not None
            else base64.b64encode(body).decode("ascii")
        ),
    }
    with open_ledger(db, create=False) as lg:
        event = lg._append_doc_event(
            doc_verb._DOC_WRITER_CAPABILITY, kind=DOC_KIND, payload=payload,
            item_id=ITEM, idempotency_key="unmarked",
        )
    return event.id


def _export_without_scan(db: str, export: Path) -> list:
    """Write every event of ``db`` as an export the way a ledger with no
    export scan wrote one. Returns the events."""
    with open_ledger(db, create=False) as lg:
        events = lg.events()
    write_event_files(export, events)
    write_watermark(export, events[-1].id)
    write_stream_digest(export, load_export(export))
    return events


def _credential_refused(event_id: int, shape: str) -> str:
    return re.escape(
        f"restore refuses event {event_id} (doc.attached): restore_doc_credential: "
        f"its bytes hold a credential shape ({shape}) and it carries no "
        "credential_override"
    )


def test_restore_refuses_an_unmarked_credential_document(db, tmp_path):
    sample = SAMPLES["aws-access-key"]
    event_id = _store_unmarked(db, sample)
    _export_without_scan(db, tmp_path / "export")
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises(LedgerError, match=_credential_refused(event_id, "aws-access-key")) as caught:
        restore_export(tmp_path / "export", dest, system="home")
    _assert_no_sample(sample, str(caught.value))
    assert not any(dest.parent.glob("ledger.db*")), "a refused restore left files behind"


def test_replay_events_refuses_an_unmarked_credential_document(db, tmp_path):
    sample = SAMPLES["private-key"]
    event_id = _store_unmarked(db, sample)
    with open_ledger(db, create=False) as lg:
        events = lg.events()
    assert events[-1].id == event_id
    with open_ledger(tmp_path / "fresh.db") as fresh:
        # The destination already holds the item; the refused batch leaves
        # it as it was.
        replay_events(fresh, events[:-1])
        before = fresh.events()
        with pytest.raises(
            LedgerError, match=_credential_refused(event_id, "private-key")
        ) as caught:
            replay_events(fresh, events[-1:])
        assert fresh.events() == before, "a refused replay changed the destination"
    _assert_no_sample(sample, str(caught.value))


def test_a_marked_credential_document_restores_and_replays(db, tmp_path, monkeypatch):
    source = write(tmp_path / "key.txt", wrap(SAMPLES["github-token"]))
    assert add(monkeypatch, db, source, "--allow-credential-shapes")[0] == 0
    with open_ledger(db, create=False) as lg:
        export_events(lg, tmp_path / "export")
        events = lg.events()
    dest = tmp_path / "restored" / "ledger.db"
    restore_export(tmp_path / "export", dest, system="home")
    [restored] = doc_events(str(dest))
    assert restored.payload["credential_override"] is True
    with open_ledger(tmp_path / "fresh.db") as fresh:
        replay_events(fresh, events)
        assert [e.payload for e in fresh.events(kind=DOC_KIND)] == [restored.payload]


@pytest.mark.parametrize("value", [False, 1, "true"])
def test_a_restore_or_replay_with_a_credential_override_other_than_true_is_refused(
    db, tmp_path, monkeypatch, value
):
    source = write(tmp_path / "key.txt", wrap(SAMPLES["openai-key"]))
    assert add(monkeypatch, db, source, "--allow-credential-shapes")[0] == 0
    export = tmp_path / "export"
    with open_ledger(db, create=False) as lg:
        export_events(lg, export)
        events = lg.events()
    document = events[-1]
    assert document.kind == DOC_KIND
    forged = dataclasses.replace(
        document, payload={**document.payload, "credential_override": value}
    )
    forged = dataclasses.replace(forged, hash=event_digest(hash_body(forged)))
    (export / event_filename(forged.id)).write_text(
        canonical_dumps(event_record(forged)) + "\n", encoding="utf-8"
    )
    dest = tmp_path / "restored" / "ledger.db"
    with pytest.raises((ValidationError, LedgerError), match="bad-type:credential_override"):
        restore_export(export, dest, system="home")
    assert not any(dest.parent.glob("ledger.db*")), "a refused restore left files behind"
    replaced = [forged if event.id == forged.id else event for event in events]
    with open_ledger(tmp_path / "fresh.db") as fresh:
        with pytest.raises(
            LedgerError,
            match=r"restore refuses event \d+ \(doc\.attached\): bad-type:credential_override",
        ):
            replay_events(fresh, replaced)
        assert fresh.events() == [], "a refused replay inserted events"


# ---- credential shapes at export -----------------------------------------------------


def _export_state(export: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(export.iterdir())}


def test_export_refuses_an_unmarked_credential_document_and_writes_nothing(
    db, tmp_path, monkeypatch
):
    export = tmp_path / "export"
    with open_ledger(db, create=False) as lg:
        export_events(lg, export)
    before = _export_state(export)
    watermark = read_watermark(export)
    assert watermark > 0
    sample = SAMPLES["openai-key"]
    # The test hook: doc add's scan sees nothing, so an unmarked credential
    # document reaches the store and then the export step.
    with monkeypatch.context() as hook:
        hook.setattr(doc_verb, "scan_credentials", lambda data: ())
        code, added, err = add(monkeypatch, db, write(tmp_path / "leak.md", wrap(sample)))
    assert code == 0, err
    assert added["data"]["credential_override"] is False
    [document] = doc_events(db)
    message = (
        f"export refuses event {document.id} (doc.attached) on item {ITEM}: "
        "export_doc_credential: its bytes hold a credential shape (openai-key) and it "
        "carries no credential_override; nothing was written"
    )
    with open_ledger(db, create=False) as lg:
        with pytest.raises(ExportDocCredential) as caught:
            export_events(lg, export)
    assert str(caught.value) == message
    assert _export_state(export) == before, "a refused export wrote a file"
    assert read_watermark(export) == watermark

    commits: list = []
    monkeypatch.setattr(export_verb, "commit_export", lambda *a, **k: commits.append(a))
    code, refused, err = cli_json(monkeypatch, db, "export", "--dir", str(export), "--commit")
    assert (code, refused["ok"], refused["error"]["code"]) == (3, False, "export_doc_credential")
    assert refused["error"]["message"] == message
    _assert_no_sample(sample, json.dumps(refused), err)
    code, out, err = cli(monkeypatch, db, "export", "--dir", str(export))
    assert (code, out) == (3, "")
    assert message in err
    _assert_no_sample(sample, out, err)
    assert commits == [], "export --commit ran after a refused export"
    assert _export_state(export) == before, "a refused export wrote a file"
    assert read_watermark(export) == watermark


def test_a_marked_credential_document_exports(db, tmp_path, monkeypatch):
    source = write(tmp_path / "key.md", wrap(SAMPLES["slack-token"]))
    assert add(monkeypatch, db, source, "--allow-credential-shapes")[0] == 0
    export = tmp_path / "export"
    code, exported, err = cli_json(monkeypatch, db, "export", "--dir", str(export))
    assert code == 0, f"a marked document was refused at export: {exported}"
    [document] = doc_events(db)
    assert (export / event_filename(document.id)).is_file()
    assert read_watermark(export) == document.id == exported["data"]["watermark"]


def test_a_clean_document_exports(db, tmp_path, monkeypatch):
    assert add(monkeypatch, db, write(tmp_path / "clean.md", b"plain notes"))[0] == 0
    export = tmp_path / "export"
    with open_ledger(db, create=False) as lg:
        result = export_events(lg, export)
    [document] = doc_events(db)
    assert (export / event_filename(document.id)).is_file()
    assert result.watermark == document.id


def test_export_refuses_a_document_whose_inline_bytes_cannot_be_scanned(db, tmp_path):
    # Fail closed: bytes that do not decode cannot be scanned, so they are
    # not exported either.
    event_id = _store_unmarked(db, b"", content_b64="!!!")
    export = tmp_path / "export"
    with open_ledger(db, create=False) as lg:
        with pytest.raises(ExportDocCredential, match="its inline bytes do not decode"):
            export_events(lg, export)
    assert not any(export.glob("*.json")), "a refused export wrote a file"
    assert event_id > 0
