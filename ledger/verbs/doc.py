"""Attach, show or verify an item's documents: content-addressed records.

A document is one ``doc.attached`` event on an item. ``doc add`` reads the
file's bytes once, computes their sha256 and size, and stores the bytes
inline (``content_b64``). Documents are inline-only: a file over
``DOC_INLINE_LIMIT`` is refused, because by-path documents are not
supported yet.

``doc add`` opens FILE one component at a time, as given (never
normalised), each open relative to the descriptor of the component before
it and none following a symlink; see ``_open_by_walk`` for the grammar. A
symlink at any component of the path as given, or one swapped in while the
walk runs, is refused (``doc_unreadable``) rather than followed. The leaf
open is non-blocking, so a FIFO cannot stall it, and the open descriptor
must be a regular file, checked before any byte is read. Nothing else about
the leaf is checked: a file with a second hard link is a regular file, and
is read like any other. FILE given as a single hyphen reads standard input
instead; that is a convenience for piping generated text, not a safer way
to attach a file. Every filename is escaped before it reaches a refusal, so
a name holding a terminal control sequence is never printed raw.

``doc add`` attaches whatever bytes FILE holds and does not vouch for where
they came from: it runs as the caller's operating-system user, so it can
attach any file that user can read, by its own path, through a hard link or
through a copy. What it guarantees is that the stored sha256 is the hash of
the stored bytes.

Before anything is written, ``doc add`` scans the bytes for credential
shapes (``ledger.credscan``). A hit is refused ``doc_credential_shape``
unless ``--allow-credential-shapes`` is given, which writes the event with
``credential_override: true``. The scan runs before the idempotency check,
so repeating an overridden add without the flag is refused, not reported
``unchanged``. No message carries the matched bytes.

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
import sys
from typing import Any

from ledger.credscan import describe as describe_credentials
from ledger.credscan import scan as scan_credentials
from ledger.restore import inline_document_bytes
from ledger.schema import (
    DOC_CREDENTIAL_OVERRIDE,
    DOC_DEFAULT_MIME,
    DOC_INLINE_LIMIT,
    DOC_KIND,
    DOC_ROLES,
)
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
CREDENTIAL_SHAPE = "doc_credential_shape"
# FILE exactly this reads standard input; a file of that name is ``./-``.
STDIN = "-"
STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"
# Every document is stored inline; the row keeps the storage and path keys
# so the ``doc show --json`` envelope stays the same shape.
STORAGE_INLINE = "inline"
_CHUNK = 1 << 16
_EVENT_ID = re.compile(r"[1-9][0-9]{0,18}")
_MAX_EVENT_ID = 2**63 - 1
_ADD_ONLY = ("file", "role", "title", "mime", "allow_credential_shapes")
# The store appends doc.attached only for a caller holding this
# (attach_document).
_DOC_WRITER_CAPABILITY = object()


def document_key(item_id: str, role: str, sha256: str) -> str:
    """The idempotency key of a document: its item, role and sha256."""
    return f"doc:{item_id}:{role}:{sha256}"


_NOT_REGULAR = "not a readable regular file"
_THROUGH_SYMLINK = "its path goes through a symlink, which is refused; pass the real path"


def _unreadable(name: str, reason: str) -> CliError:
    return CliError(
        f"cannot read {text_cell(name)}: {reason}",
        code="doc_unreadable",
        exit_code=EXIT_REFUSED,
    )


def _walk_plan(name: str) -> tuple[bool, list[str], str]:
    """``(absolute, directory components, leaf)`` for FILE as given.

    FILE is split on ``/`` and never normalised. Empty components (a
    leading ``/`` or a repeated one, as in ``//a.md`` or ``a//b.md``) and
    ``.`` components are skipped. Refused ``doc_unreadable``: an empty FILE;
    a ``..`` component anywhere; and a FILE whose last component is empty or
    ``.`` (it ends in ``/`` or ``/.``, so it names a directory).
    """
    if name == "":
        raise CliError(
            "cannot read FILE: it is empty", code="doc_unreadable", exit_code=EXIT_REFUSED
        )
    parts = name.split("/")
    if ".." in parts:
        raise _unreadable(
            name, "its path holds a '..' component, which is refused; pass the real path"
        )
    leaf = parts[-1]
    if leaf in ("", "."):
        raise _unreadable(name, "it names a directory, not a file")
    directories = [part for part in parts[:-1] if part not in ("", ".")]
    return name.startswith("/"), directories, leaf


def _no_follow_flags() -> tuple[int, int] | None:
    """``(O_NOFOLLOW, O_DIRECTORY)``, or None when the platform lacks either
    flag or cannot open relative to a directory descriptor."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or os.open not in os.supports_dir_fd:
        return None
    return nofollow, directory


def _is_symlink(part: str, dir_fd: int) -> bool:
    """Whether ``part`` under ``dir_fd`` is a symlink. Chooses a refusal's
    wording only; the refusal itself is the failed no-follow open."""
    try:
        return stat.S_ISLNK(os.stat(part, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except (OSError, ValueError, NotImplementedError):
        return False


def _open_component(name: str, part: str, flags: int, dir_fd: int) -> int:
    try:
        return os.open(part, flags, dir_fd=dir_fd)
    except (OSError, ValueError):
        pass
    raise _unreadable(name, _THROUGH_SYMLINK if _is_symlink(part, dir_fd) else _NOT_REGULAR)


def _open_by_walk(name: str) -> int:
    """An open descriptor on the leaf of FILE, reached without following a
    symlink at any component.

    The walk starts from a descriptor on ``/`` (absolute FILE) or ``.``
    (relative FILE); the starting directory itself is not checked. Each
    directory component is opened ``O_DIRECTORY | O_NOFOLLOW`` relative to
    the descriptor before it, and the leaf ``O_NOFOLLOW | O_NONBLOCK``
    relative to its parent's; each parent descriptor is closed once its
    child is open, and every descriptor is closed on every refusal. The
    grammar of FILE is ``_walk_plan``'s. A platform with no no-follow,
    directory-only or descriptor-relative open is refused before any open.

    The leaf descriptor is then checked before any byte is read
    (``_leaf_refusal``): its ``fstat`` must show a regular file. Nothing
    else about the leaf is checked, not its link count and not its name
    after the open: a hard link is a second name for a regular file, not a
    symlink, and the walk reads it like any other file. The parent
    descriptor is closed after the check and on every refusal.
    """
    absolute, directories, leaf = _walk_plan(name)
    flags = _no_follow_flags()
    if flags is None:
        raise _unreadable(
            name, "this platform cannot open a path without following symlinks"
        )
    nofollow, directory = flags
    try:
        fd = os.open("/" if absolute else ".", os.O_RDONLY | directory)
    except OSError as exc:
        raise _unreadable(name, _NOT_REGULAR) from exc
    try:
        for part in directories:
            child = _open_component(name, part, os.O_RDONLY | directory | nofollow, fd)
            os.close(fd)
            fd = child
        leaf_fd = _open_component(
            name, leaf, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0), fd
        )
        verified = False
        try:
            refusal = _leaf_refusal(leaf_fd)
            if refusal is not None:
                raise _unreadable(name, refusal)
            verified = True
            return leaf_fd
        finally:
            if not verified:
                os.close(leaf_fd)
    finally:
        os.close(fd)


def _leaf_refusal(fd: int) -> str | None:
    """Why the walk's leaf is refused, or None: the ``fstat`` of the leaf
    descriptor must show a regular file. Nothing else is checked."""
    try:
        info = os.fstat(fd)
    except OSError:
        return _NOT_REGULAR
    if not stat.S_ISREG(info.st_mode):
        return _NOT_REGULAR
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


def _display_name(name: str) -> str:
    """FILE as a message names it: escaped, and ``standard input`` for ``-``."""
    return "standard input" if name == STDIN else text_cell(name)


def _read_stdin() -> bytes:
    """Standard input's binary stream, read until it ends or holds more than
    ``DOC_INLINE_LIMIT`` bytes; over the limit is a usage error."""
    stream = getattr(sys.stdin, "buffer", None) if sys.stdin is not None else None
    if stream is None:
        raise CliError(
            "cannot read standard input: it is not a binary stream",
            code="doc_unreadable",
            exit_code=EXIT_REFUSED,
        )
    data = bytearray()
    try:
        while len(data) <= DOC_INLINE_LIMIT:
            chunk = stream.read(DOC_INLINE_LIMIT + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise CliError(
            f"cannot read standard input: {text_cell(reason)}",
            code="doc_unreadable",
            exit_code=EXIT_REFUSED,
        ) from exc
    if len(data) > DOC_INLINE_LIMIT:
        raise UsageError(
            f"standard input is over the {DOC_INLINE_LIMIT}-byte inline limit; "
            "documents are stored inline, and by-path documents are not supported yet"
        )
    return bytes(data)


def _read_input(name: str) -> bytes:
    """The bytes of the file named on the command line, read once.

    FILE exactly ``-`` reads standard input (``_read_stdin``); a file whose
    name is a single hyphen is passed as ``./-``. Any other FILE is opened
    by ``_open_by_walk``, so refused ``doc_unreadable`` when any component of
    the path as given is a symlink or ``..``, when it ends in ``/``, when it
    is empty, and when it is not a readable regular file (missing, a
    directory, a FIFO); the walk checks the leaf before any byte is read.
    The bytes are read from the walk's leaf descriptor. A file over
    ``DOC_INLINE_LIMIT`` is a usage error. The name is escaped in every
    refusal.
    """
    if name == STDIN:
        return _read_stdin()
    fd = _open_by_walk(name)
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
    allow_credential_shapes: bool = False,
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

    The bytes are scanned for credential shapes first, before the duplicate
    check and before any write. A hit is refused ``doc_credential_shape``,
    naming FILE and each shape with its byte offset but never the matched
    bytes, unless ``allow_credential_shapes`` is set; then the event carries
    ``credential_override: true``. The flag on bytes with no hit writes no
    marker.
    """
    data = _read_input(name)
    hits = scan_credentials(data)
    if hits and not allow_credential_shapes:
        raise CliError(
            f"{_display_name(name)} holds a credential shape "
            f"({describe_credentials(hits)}); nothing was written. Pass "
            "--allow-credential-shapes to attach it anyway",
            code=CREDENTIAL_SHAPE,
            exit_code=EXIT_REFUSED,
        )
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
    if hits:
        payload[DOC_CREDENTIAL_OVERRIDE] = True
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
        "credential_override": payload.get(DOC_CREDENTIAL_OVERRIDE) is True,
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
        "no symlink anywhere in its path); - reads standard input",
    )
    parser.add_argument("--role", choices=DOC_ROLES, help="add: what the document is")
    parser.add_argument("--title", help="add: a one-line title")
    parser.add_argument(
        "--mime",
        help=f"add: the media type (default {DOC_DEFAULT_MIME}; {MARKDOWN_MIME} for .md)",
    )
    parser.add_argument(
        "--allow-credential-shapes",
        action="store_true",
        help="add: attach bytes holding a known credential shape, marking the "
        "document credential_override",
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
            allow_credential_shapes=args.allow_credential_shapes,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    row = document_row(event)
    text = f"doc\t{args.item}\t{row['role']}\t{row['storage']}\t" + (
        f"event={event.id}" if created else f"unchanged event={event.id}"
    )
    if row["credential_override"]:
        text += "\tcredential-override"
    return Result(
        data={
            "item_id": args.item,
            "unchanged": not created,
            "credential_override": row["credential_override"],
            "document": row,
        },
        text=text,
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
    if row["credential_override"]:
        fields.append("credential-override")
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
