"""Attach, show or verify an item's documents: content-addressed records.

A document is one ``doc.attached`` event on an item. ``doc add`` reads the
file's bytes once, computes their sha256 and size, and stores the bytes
inline (``content_b64``). Documents are inline-only: a file over
``DOC_INLINE_LIMIT`` is refused, because by-path documents are not
supported yet.

``doc add`` refuses (``doc_unreadable``) a FILE whose path holds a symlink
anywhere, leaf or parent: its resolved path must equal its absolute path,
checked once before the open. The open itself never follows the leaf
(``O_NOFOLLOW``; where the platform has no such flag, the leaf is
``lstat``-ed first, anything but a regular file is refused there, and the
opened file must be that same device and inode). It is non-blocking, so a
FIFO cannot stall it, and the open descriptor must be a regular file. Every
filename is escaped before it reaches a refusal, so a name holding a
terminal control sequence is never printed raw.

``doc show`` and ``doc verify`` read only the stored inline bytes and
recompute their sha256 and size; they never touch the filesystem. They are
reads and run without ``LEDGER_HARNESS``; ``doc add`` is the one write and
refuses without it, as every write verb does.

Adding the same item, role and sha256 again writes nothing and reports
``unchanged``; the idempotency key is derived from those three, so a retried
add lands once. The same bytes under another role are a second document.

Nothing is printed as a document's content, or reported ``ok``, unless its
stored bytes hash to the stored sha256 and size: a mismatch is refused,
never printed.

``attach_document`` is the only writer of ``doc.attached``: the store
reserves the kind, so ``ledger append`` and ``append-many`` refuse it and
every stored sha256 is one the ledger computed from the bytes it read.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from typing import Any

from ledger.restore import inline_document_bytes
from ledger.schema import DOC_DEFAULT_MIME, DOC_INLINE_LIMIT, DOC_KIND, DOC_ROLES
from ledger.store import Event, Ledger
from ledger.verbs import (
    EXIT_INTEGRITY,
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    actor_kwargs,
    open_db,
)
from ledger.verbs.events import text_cell

ACTIONS = ("add", "show", "verify")
MARKDOWN_MIME = "text/markdown"
VERIFY_FAILED = "doc_verify_failed"
STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"
# Every document is stored inline; the row keeps the storage and path keys
# so the ``doc show --json`` envelope stays the same shape.
STORAGE_INLINE = "inline"
_CHUNK = 1 << 16
_EVENT_ID = re.compile(r"[1-9][0-9]{0,18}")
_MAX_EVENT_ID = 2**63 - 1
_ADD_ONLY = ("file", "role", "title", "mime")
# The store appends doc.attached only for a caller holding this
# (attach_document).
_DOC_WRITER_CAPABILITY = object()


def document_key(item_id: str, role: str, sha256: str) -> str:
    """The idempotency key of a document: its item, role and sha256."""
    return f"doc:{item_id}:{role}:{sha256}"


def _open_regular(path: str) -> int | None:
    """An open descriptor on ``path`` when it is a regular file, else None.

    The leaf is never followed: it is opened ``O_NOFOLLOW``, so a symlink
    fails to open. Where the platform has no ``O_NOFOLLOW``, the leaf is
    ``lstat``-ed first (anything but a regular file, a symlink included, is
    refused there, before any open) and the opened file must be that same
    device and inode. Opened non-blocking so a FIFO cannot stall the read;
    the type is checked on the open descriptor, so a swap after a check
    cannot slip in.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    leaf = None
    if not nofollow:
        try:
            leaf = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(leaf.st_mode):
            return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | nofollow)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if stat.S_ISREG(opened.st_mode) and (
            leaf is None or (opened.st_dev, opened.st_ino) == (leaf.st_dev, leaf.st_ino)
        ):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _read_at_most(fd: int, limit: int) -> bytes:
    """What ``fd`` reads, stopping once it holds more than ``limit`` bytes."""
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(_CHUNK, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _read_input(name: str) -> bytes:
    """The bytes of the file named on the command line, read once.

    Refused ``doc_unreadable`` when any component of its path is a symlink
    (its resolved path is not its absolute path), and when it is not a
    readable regular file (missing, a directory, a FIFO, a symlinked leaf).
    A file over ``DOC_INLINE_LIMIT`` is a usage error. The name is escaped
    in every refusal.
    """
    try:
        absolute = os.path.abspath(name)
        resolved = os.path.realpath(name)
    except (OSError, ValueError):
        absolute = resolved = None
    if absolute is not None and resolved != absolute:
        raise CliError(
            f"cannot read {text_cell(name)}: its path goes through a symlink, "
            "which is refused; pass the real path",
            code="doc_unreadable",
            exit_code=EXIT_REFUSED,
        )
    fd = _open_regular(absolute) if absolute is not None else None
    if fd is None:
        raise CliError(
            f"cannot read {text_cell(name)}: not a readable regular file",
            code="doc_unreadable",
            exit_code=EXIT_REFUSED,
        )
    try:
        data = _read_at_most(fd, DOC_INLINE_LIMIT)
    except OSError as exc:
        raise CliError(
            f"cannot read {text_cell(name)}: {text_cell(exc.strerror or str(exc))}",
            code="doc_unreadable",
            exit_code=EXIT_REFUSED,
        ) from exc
    finally:
        os.close(fd)
    if len(data) > DOC_INLINE_LIMIT:
        raise UsageError(
            f"{text_cell(name)} is over the {DOC_INLINE_LIMIT}-byte inline limit; "
            "documents are stored inline, and by-path documents are not supported yet"
        )
    return data


def default_mime(name: str) -> str:
    return MARKDOWN_MIME if name.lower().endswith(".md") else DOC_DEFAULT_MIME


def attach_document(
    lg: Ledger,
    item_id: str,
    name: str,
    *,
    role: str,
    mime: str | None = None,
    title: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> tuple[Event, bool]:
    """Read ``name``, hash it, and attach its bytes inline to ``item_id``.

    Returns ``(event, created)``: the new event and True, or the stored
    document with the same item, role and sha256 and False (nothing is
    written). The item check and the duplicate check run inside the write
    transaction.
    """
    data = _read_input(name)
    sha256 = hashlib.sha256(data).hexdigest()
    payload: dict[str, Any] = {
        "role": role,
        "sha256": sha256,
        "size": len(data),
        "mime": mime if mime is not None else default_mime(name),
        "content_b64": base64.b64encode(data).decode("ascii"),
    }
    if title is not None:
        payload["title"] = title
    with lg.transaction():
        if lg.item(item_id) is None:
            raise CliError(
                f"item not found: {item_id}", code="not_found", exit_code=EXIT_NOT_FOUND
            )
        for event in lg.events(item_id=item_id, kind=DOC_KIND):
            if event.payload.get("role") == role and event.payload.get("sha256") == sha256:
                return event, False
        event = lg._append_doc_event(
            _DOC_WRITER_CAPABILITY,
            kind=DOC_KIND,
            payload=payload,
            item_id=item_id,
            idempotency_key=document_key(item_id, role, sha256),
            actor=actor,
            harness=harness,
            model=model,
            session=session,
        )
    return event, True


def document_row(event: Event) -> dict[str, Any]:
    """One document as ``doc show --json`` prints it."""
    payload = event.payload
    return {
        "event_id": event.id,
        "item_id": event.item_id,
        "role": payload.get("role"),
        "title": payload.get("title"),
        "mime": payload.get("mime"),
        "size": payload.get("size"),
        "sha256": payload.get("sha256"),
        "storage": STORAGE_INLINE,
        "path": None,
    }


def _stored_bytes(event: Event) -> bytes | None:
    """The document's stored inline bytes when they hash to its stored
    sha256 and size; None when they do not, or do not decode."""
    payload = event.payload
    try:
        data = inline_document_bytes(payload)
    except ValueError:
        return None
    if hashlib.sha256(data).hexdigest() != payload.get("sha256"):
        return None
    if len(data) != payload.get("size"):
        return None
    return data


def verify_status(event: Event) -> str:
    """``ok`` when the stored bytes hash to the stored sha256 and size, else
    ``mismatch``. Reads the event only, never the filesystem."""
    return STATUS_OK if _stored_bytes(event) is not None else STATUS_MISMATCH


def verified_content(event: Event) -> bytes:
    """The document's bytes, only after they hash to its stored sha256 and
    size; otherwise refused ``doc_mismatch`` (exit 4) and no bytes are
    returned."""
    data = _stored_bytes(event)
    if data is None:
        raise CliError(
            f"document {event.id}: its inline bytes do not hash to the stored "
            "sha256; nothing printed",
            code="doc_mismatch",
            exit_code=EXIT_INTEGRITY,
        )
    return data


def add_arguments(parser) -> None:
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("item", nargs="?", help="the item (verify: optional)")
    parser.add_argument(
        "file",
        nargs="?",
        help=f"add: the file to attach, inline (at most {DOC_INLINE_LIMIT} bytes; "
        "no symlink anywhere in its path)",
    )
    parser.add_argument("--role", choices=DOC_ROLES, help="add: what the document is")
    parser.add_argument("--title", help="add: a one-line title")
    parser.add_argument(
        "--mime",
        help=f"add: the media type (default {DOC_DEFAULT_MIME}; {MARKDOWN_MIME} for .md)",
    )
    parser.add_argument("--id", dest="event_id", help="show, verify: one document by event id")
    parser.add_argument(
        "--content",
        action="store_true",
        help="show: write that document's verified bytes to stdout (needs --id)",
    )


def _parse_event_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    if _EVENT_ID.fullmatch(raw) is None or int(raw) > _MAX_EVENT_ID:
        raise UsageError(f"--id must be an event id such as 7, got {raw!r}")
    return int(raw)


def _refuse_flags(args, names: tuple[str, ...], action: str) -> None:
    given = [name for name in names if getattr(args, name) not in (None, False)]
    if given:
        flags = ", ".join(
            "FILE" if name == "file" else "--" + name.replace("_", "-") for name in given
        )
        raise UsageError(f"doc {action} does not take {flags}")


def _run_add(args, ctx: Context) -> Result:
    # The verb's one write: it needs the writer identity, refused exactly as
    # the CLI refuses a write verb without it. show and verify are reads, so
    # the verb is not ``writes`` and this action checks for itself.
    if not ctx.harness:
        raise CliError(
            "LEDGER_HARNESS is required for writes; set it, e.g. "
            "LEDGER_HARNESS=claude-code",
            code="missing_harness",
            exit_code=EXIT_REFUSED,
        )
    _refuse_flags(args, ("event_id", "content"), "add")
    if args.item is None or args.file is None:
        raise UsageError("doc add needs ITEM and FILE")
    if args.role is None:
        raise UsageError(f"doc add needs --role ({', '.join(DOC_ROLES)})")
    lg = open_db(ctx, write=True)
    try:
        event, created = attach_document(
            lg,
            args.item,
            args.file,
            role=args.role,
            mime=args.mime,
            title=args.title,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    row = document_row(event)
    return Result(
        data={"item_id": args.item, "unchanged": not created, "document": row},
        text=f"doc\t{args.item}\t{row['role']}\t{row['storage']}\t"
        + (f"event={event.id}" if created else f"unchanged event={event.id}"),
    )


def _documents(lg: Ledger, item_id: str | None, event_id: int | None) -> list[Event]:
    if item_id is not None and lg.item(item_id) is None:
        raise CliError(f"item not found: {item_id}", code="not_found", exit_code=EXIT_NOT_FOUND)
    events = lg.events(item_id=item_id, kind=DOC_KIND)
    if event_id is None:
        return events
    chosen = [event for event in events if event.id == event_id]
    if not chosen:
        where = f"item {item_id} has" if item_id is not None else "the ledger has"
        raise CliError(
            f"{where} no document {event_id}", code="not_found", exit_code=EXIT_NOT_FOUND
        )
    return chosen


def _show_line(row: dict[str, Any]) -> str:
    fields = [
        str(row["event_id"]),
        str(row["role"]),
        str(row["size"]),
        str(row["sha256"])[:12],
        str(row["storage"]),
    ]
    if row["title"] is not None:
        fields.append(str(row["title"]))
    return "\t".join(fields)


def _run_show(args, ctx: Context) -> Result:
    _refuse_flags(args, _ADD_ONLY, "show")
    if args.item is None:
        raise UsageError("doc show needs ITEM")
    event_id = _parse_event_id(args.event_id)
    if args.content and event_id is None:
        raise UsageError("doc show --content needs --id N")
    lg = open_db(ctx, write=False)
    try:
        events = _documents(lg, args.item, event_id)
    finally:
        lg.close()
    if args.content:
        [event] = events
        data = verified_content(event)
        document = {**document_row(event), "content_b64": base64.b64encode(data).decode("ascii")}
        return Result(data={"document": document}, raw=data)
    rows = [document_row(event) for event in events]
    text = "\n".join(_show_line(row) for row in rows) or f"no documents for {args.item}"
    return Result(data={"item_id": args.item, "documents": rows}, text=text)


def _run_verify(args, ctx: Context) -> Result:
    _refuse_flags(args, _ADD_ONLY + ("content",), "verify")
    event_id = _parse_event_id(args.event_id)
    lg = open_db(ctx, write=False)
    try:
        events = _documents(lg, args.item, event_id)
    finally:
        lg.close()
    rows = [
        {
            "event_id": event.id,
            "item_id": event.item_id,
            "sha256": event.payload.get("sha256"),
            "status": verify_status(event),
        }
        for event in events
    ]
    failed = [row for row in rows if row["status"] != STATUS_OK]
    data = {"documents": rows}
    if failed:
        lines = [f"doc verify failed: {len(failed)} of {len(rows)} documents"]
        lines.extend(
            f"{row['event_id']}\t{row['status']}\t{row['item_id']}" for row in failed
        )
        return Result(
            data=data,
            text="\n".join(lines),
            exit_code=EXIT_INTEGRITY,
            error_code=VERIFY_FAILED,
        )
    return Result(data=data, text=f"ok documents={len(rows)}")


def run(args, ctx: Context) -> Result:
    if args.action == "add":
        return _run_add(args, ctx)
    if args.action == "show":
        return _run_show(args, ctx)
    return _run_verify(args, ctx)


VERB = Verb(
    name="doc",
    help="add, show or verify an item's content-addressed documents",
    add_arguments=add_arguments,
    run=run,
    # show and verify are reads (the mods pane reads documents without a
    # harness); add is the one write and checks LEDGER_HARNESS itself.
    writes=False,
)
