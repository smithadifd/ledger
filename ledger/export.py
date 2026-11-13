"""Export ledger events as per-event canonical JSON files.

The watermark is a sidecar file ``watermark.json`` in the export directory,
not a row in ``events``. It stores ``{"id": <last exported event id>}`` and
advances only after every new event file has been fsynced. A missing
sidecar means id 0 for incremental exports; ``verify_export`` refuses an
export that holds event files without one.

``export --commit`` commits in the configured export directory only and never
pushes. Pushing is ``export --push``.

Before any event file is written, every pending inline ``doc.attached``
event's bytes are scanned for credential shapes (``ledger.credscan``). A hit
on an event without ``credential_override: true`` refuses the whole export
(``ExportDocCredential``): no file is written and the watermark does not
move. A by-path document's event file carries its stored path, sha256 and
size and no bytes, so export scans the stored path and never opens a document
file. ``commit_export`` stages the export directory into a private
temporary index and scans the exact tree it will commit, and the new commit
object, before any ref moves; ``push_export`` scans every commit, path and
blob reachable from the pinned branch commit (the whole history) and then
pushes exactly that commit, by its full sha, to the branch's push remote.
Both apply one rule: a file at an event name must be the canonical bytes of
a valid event record and passes the ``doc.attached`` check above, and every
other file, every path name, every commit object and the branch name must
hold no credential shape.

That export is the backup stream: restore replays it and checks the hash
chain, so it keeps every event. ``item_export`` is the per-item export a
reader or the next seat pulls from; it leaves out the item's ephemeral
events (``schema.DURABILITY``) recorded before its latest close.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .credscan import scan as scan_credentials
from .credscan import shape_names
from .store import (
    GENESIS_HASH,
    ChainBroken,
    Event,
    Ledger,
    LedgerError,
    canonical_dumps,
    check_claim_sequence,
    check_expected_system,
    event_digest,
)
from .schema import (
    DOC_CREDENTIAL_OVERRIDE,
    DOC_KIND,
    is_ephemeral,
    validate_item_id,
    validate_payload,
)

WATERMARK_NAME = "watermark.json"
STREAM_DIGEST_NAME = "stream-digest.json"
EVENT_NAME = re.compile(r"^(\d{16})\.json$")
DEFAULT_MAX_EVENTS = 100
DEFAULT_MAX_HOURS = 24.0


EXPORT_DOC_CREDENTIAL = "export_doc_credential"


class ExportRefusal(LedgerError):
    """A refusal by export, ``commit_export`` or ``push_export``, with a
    named ``code``. The CLI maps each to exit 3."""

    code = "export_refused"


class ExportDocCredential(ExportRefusal):
    """A document holds a credential shape and carries no
    ``credential_override``: at export nothing is written; at commit or push
    nothing is committed or sent."""

    code = EXPORT_DOC_CREDENTIAL


class ExportHeadDetached(ExportRefusal):
    """HEAD is detached or names a ref outside ``refs/heads/``."""

    code = "export_head_detached"


class ExportPushUnborn(ExportRefusal):
    """The branch has no commit, so there is nothing to push."""

    code = "export_push_unborn"


class ExportPushUnconfigured(ExportRefusal):
    """No push remote is configured, or the value starts with a hyphen."""

    code = "export_push_unconfigured"


class ExportPushMultipleUrls(ExportRefusal):
    """The push remote has more than one push destination (best effort)."""

    code = "export_push_multiple_urls"


class ExportRepoShallow(ExportRefusal):
    """The export repository is shallow."""

    code = "export_repo_shallow"


class ExportHistoryIncomplete(ExportRefusal):
    """A listed commit names a parent the history listing does not hold."""

    code = "export_history_incomplete"


class ExportFileCredential(ExportRefusal):
    """A credential shape in a file that is not an event, a path name or a
    commit object."""

    code = "export_file_credential"


class ExportEventInvalid(ExportRefusal):
    """A file at an event name is not a valid event record."""

    code = "export_event_invalid"


class ExportObjectUnreadable(ExportRefusal):
    """A missing object, a wrong object type, a gitlink, or a branch that
    does not hold a commit."""

    code = "export_object_unreadable"


class ExportHeadMoved(ExportRefusal):
    """At the swap the branch no longer holds the commit the step read, or
    HEAD no longer names the branch."""

    code = "export_head_moved"


class ExportCommitAncestry(ExportRefusal):
    """The new commit's tree or parents are not the scanned tree and the
    old head."""

    code = "export_commit_ancestry"


class ExportGitFailed(ExportRefusal):
    """Any other git command failure before the push, or in the commit."""

    code = "export_git_failed"


class ExportPushFailed(ExportRefusal):
    """The push command ran and failed; its outcome is unknown."""

    code = "export_push_failed"


class ExportBranchCredential(ExportRefusal):
    """The branch name holds a credential shape; it is never printed."""

    code = "export_branch_credential"


class ExportCommitSignFailed(ExportRefusal):
    """``commit.gpgSign`` is true and ``git commit-tree -S`` failed."""

    code = "export_commit_sign_failed"


class LagExceeded(LedgerError):
    """Unexported events exceed the configured event count or age."""

    def __init__(self, lag: Lag, max_events: int, max_hours: float):
        self.lag = lag
        self.max_events = max_events
        self.max_hours = max_hours
        super().__init__(
            f"export lag {lag.events} events, {lag.hours:.1f} hours "
            f"(limits {max_events} events, {max_hours} hours)"
        )


@dataclass(frozen=True)
class ExportResult:
    watermark_before: int
    watermark: int
    exported: int


@dataclass(frozen=True)
class Lag:
    watermark: int
    events: int
    hours: float
    oldest_recorded_at: str | None


@dataclass(frozen=True)
class CommitResult:
    sha: str | None
    pushed: bool = False
    # Whether the real index was set to the new commit's tree (best effort).
    index_refreshed: bool = False


def event_filename(event_id: int) -> str:
    return f"{event_id:016d}.json"


def event_record(event: Event) -> dict[str, Any]:
    return asdict(event)


def hash_body(event: Event) -> dict[str, Any]:
    """Fields covered by ``event.hash``; must match the store's hash body."""
    return {
        "actor": event.actor,
        "channel": event.channel,
        "harness": event.harness,
        "id": event.id,
        "idempotency_key": event.idempotency_key,
        "item_id": event.item_id,
        "kind": event.kind,
        "model": event.model,
        "payload": event.payload,
        "payload_version": event.payload_version,
        "prev_hash": event.prev_hash,
        "provenance_class": event.provenance_class,
        "recorded_at": event.recorded_at,
        "session": event.session,
    }


def canonical_stream(events: list[Event]) -> bytes:
    if not events:
        return b""
    return ("\n".join(canonical_dumps(event_record(event)) for event in events) + "\n").encode(
        "utf-8"
    )


_REQUIRED_STR_FIELDS = ("kind", "idempotency_key", "recorded_at", "prev_hash", "hash")
_NULLABLE_STR_FIELDS = (
    "item_id",
    "actor",
    "harness",
    "model",
    "session",
    "channel",
    "provenance_class",
)


def _record_int(record: Mapping[str, Any], field: str) -> int:
    if field not in record:
        raise ChainBroken(f"exported event is missing field {field}")
    value = record[field]
    if type(value) is not int:
        raise ChainBroken(f"exported event field {field} must be an integer")
    return value


def event_from_record(record: Mapping[str, Any]) -> Event:
    """Build an Event from an exported record; any bad shape is ``ChainBroken``.

    A missing field, or one of the wrong type, names the field. ``load_export``
    adds the event file name to the message.
    """
    if "payload" not in record:
        raise ChainBroken("exported event is missing field payload")
    payload = record["payload"]
    if type(payload) is not dict:
        raise ChainBroken("exported event payload is not an object")
    event_id = _record_int(record, "id")
    payload_version = _record_int(record, "payload_version")
    for field in _REQUIRED_STR_FIELDS:
        if field not in record:
            raise ChainBroken(f"exported event is missing field {field}")
        if type(record[field]) is not str:
            raise ChainBroken(f"exported event field {field} must be a string")
    for field in _NULLABLE_STR_FIELDS:
        if field not in record:
            raise ChainBroken(f"exported event is missing field {field}")
        if record[field] is not None and type(record[field]) is not str:
            raise ChainBroken(f"exported event field {field} must be a string or null")
    return Event(
        id=event_id,
        kind=record["kind"],
        payload_version=payload_version,
        payload=payload,
        item_id=record["item_id"],
        idempotency_key=record["idempotency_key"],
        actor=record["actor"],
        harness=record["harness"],
        model=record["model"],
        session=record["session"],
        channel=record["channel"],
        provenance_class=record["provenance_class"],
        recorded_at=record["recorded_at"],
        prev_hash=record["prev_hash"],
        hash=record["hash"],
    )


def resolve_export_dir(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    return resolved.resolve()


def read_watermark(export_dir: Path) -> int:
    path = export_dir / WATERMARK_NAME
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise LedgerError(f"unreadable watermark: {exc}") from exc
    if type(data) is not dict or "id" not in data:
        raise LedgerError("watermark.json must be an object with id")
    value = data["id"]
    if type(value) is not int:
        raise LedgerError("watermark id must be an integer")
    if value < 0:
        raise LedgerError("watermark id must be >= 0")
    return value


def write_watermark(export_dir: Path, event_id: int) -> None:
    payload = canonical_dumps({"id": event_id}).encode("utf-8") + b"\n"
    _atomic_write(export_dir / WATERMARK_NAME, payload)


def stream_digest(events: list[Event]) -> str:
    return hashlib.sha256(canonical_stream(events)).hexdigest()


def write_stream_digest(export_dir: Path, events: list[Event]) -> None:
    payload = canonical_dumps(
        {"algorithm": "sha256", "digest": stream_digest(events), "events": len(events)}
    ).encode("utf-8") + b"\n"
    _atomic_write(export_dir / STREAM_DIGEST_NAME, payload)


def verify_stream_digest(export_dir: Path, events: list[Event]) -> str:
    path = resolve_export_dir(export_dir) / STREAM_DIGEST_NAME
    if not path.is_file():
        raise LedgerError(f"export stream digest is missing: {STREAM_DIGEST_NAME}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise LedgerError(f"unreadable export stream digest: {exc}") from exc
    if type(record) is not dict:
        raise LedgerError("export stream digest must be an object")
    if record.get("algorithm") != "sha256":
        raise LedgerError("export stream digest algorithm must be sha256")
    if type(record.get("events")) is not int or record["events"] != len(events):
        raise LedgerError("export stream digest event count does not match")
    expected = stream_digest(events)
    if type(record.get("digest")) is not str or record["digest"] != expected:
        raise LedgerError("export stream digest does not match")
    return expected


_TMP_ATTEMPTS = 16
_TMP_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _create_temp(directory: Path, name: str) -> tuple[int, str]:
    """Create a fresh temp file next to the target and return (fd, path).

    O_EXCL makes the create fail on any pre-existing name, including a
    symlink, so nothing is written through it. Mode 0o666 lets the kernel
    apply the caller's umask atomically; the process-wide mask is never read
    or changed, so no other thread can see a transient mask.
    """
    for _ in range(_TMP_ATTEMPTS):
        tmp_name = str(directory / f"{name}.{secrets.token_hex(8)}.tmp")
        try:
            return os.open(tmp_name, _TMP_FLAGS, 0o666), tmp_name
        except FileExistsError:
            continue
    raise LedgerError(f"could not create a temporary file for {name}")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = _create_temp(path.parent, path.name)
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_event_files(export_dir: Path, events: list[Event]) -> list[Path]:
    """Write canonical JSON files for ``events``. Does not touch the watermark."""
    export_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for event in events:
        path = export_dir / event_filename(event.id)
        body = canonical_dumps(event_record(event)).encode("utf-8") + b"\n"
        _atomic_write(path, body)
        written.append(path)
    return written


def refuse_unmarked_credentials(events: Sequence[Event]) -> None:
    """Refuse ``ExportDocCredential`` when a ``doc.attached`` event in
    ``events`` holds a credential shape and carries no
    ``credential_override: true``. The message names the event, its item and
    the shapes, never the matched bytes. A document whose inline bytes do
    not decode cannot be scanned, and is refused the same way.

    A by-path document (one carrying ``path`` and no ``content_b64``) holds
    no bytes, so its file is never opened here; its event file carries the
    stored path, so the path's UTF-8 bytes are scanned instead, and an
    unmarked hit is refused the same way, naming the shapes as in the path
    and never the path. A stored path that is not UTF-8 text cannot be
    scanned and is refused too. An event carrying both keys is scanned as
    inline (the schema refuses that shape, so it is never stored).

    The decision for one event is ``unmarked_credential_reason``, which the
    commit and push scans share."""
    for event in events:
        reason = unmarked_credential_reason(event)
        if reason is None:
            continue
        where = f"export refuses event {event.id} ({event.kind}) on item {event.item_id}"
        tail = (
            ", and nothing was written"
            if reason.endswith(_PATH_NOT_SHOWN)
            else "; nothing was written"
        )
        raise ExportDocCredential(f"{where}: {EXPORT_DOC_CREDENTIAL}: {reason}{tail}")


_PATH_NOT_SHOWN = "the path is not shown"


def unmarked_credential_reason(event: Event) -> str | None:
    """Why export refuses ``event``, or None when it passes.

    Only a ``doc.attached`` event without ``credential_override: true`` is
    refused: when its inline bytes hold a credential shape or do not decode,
    or when it is a by-path document whose stored path holds a shape or is
    not UTF-8 text. The reason names the shapes, never the bytes or the
    path."""
    from .restore import inline_document_bytes

    if event.kind != DOC_KIND:
        return None
    if event.payload.get(DOC_CREDENTIAL_OVERRIDE) is True:
        return None
    if "path" in event.payload and "content_b64" not in event.payload:
        path = event.payload["path"]
        try:
            encoded = path.encode("utf-8") if type(path) is str else None
        except UnicodeEncodeError:
            encoded = None
        if encoded is None:
            return (
                "its stored path is not UTF-8 text, so it cannot be scanned for "
                "credential shapes"
            )
        path_hits = scan_credentials(encoded)
        if path_hits:
            return (
                f"its stored path holds a credential shape ({shape_names(path_hits)} in "
                f"the path) and it carries no credential_override; {_PATH_NOT_SHOWN}"
            )
        return None
    try:
        data = inline_document_bytes(event.payload)
    except ValueError:
        return (
            "its inline bytes do not decode, so they cannot be scanned for credential "
            "shapes"
        )
    hits = scan_credentials(data)
    if hits:
        return (
            f"its bytes hold a credential shape ({shape_names(hits)}) and it carries no "
            "credential_override"
        )
    return None


def export_events(lg: Ledger, export_dir: Path) -> ExportResult:
    """Write the events after the watermark, then move the watermark.

    Every pending document is scanned first (``refuse_unmarked_credentials``):
    a refusal writes no event file and leaves the watermark where it was.
    """
    export_dir = resolve_export_dir(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    watermark_before = read_watermark(export_dir)
    pending = lg.events(after_id=watermark_before)
    refuse_unmarked_credentials(pending)
    write_event_files(export_dir, pending)
    if pending:
        write_watermark(export_dir, pending[-1].id)
    write_stream_digest(export_dir, load_export(export_dir))
    return ExportResult(
        watermark_before=watermark_before,
        watermark=read_watermark(export_dir),
        exported=len(pending),
    )


def _closes_item(event: Event) -> bool:
    if event.kind == "loop.closed":
        return True
    if event.kind in ("item.created", "item.state"):
        return event.payload.get("status") == "closed"
    return False


def decay_boundary(item_events: Sequence[Event], closed: bool) -> float:
    """The id below which the item's ephemeral events have decayed.

    Decay is per close: an ephemeral event recorded before any close of its
    item stays decayed even after a reopen, and one recorded after a reopen
    shows until the item closes again. While the item is closed, all of them
    have decayed.
    """
    if closed:
        return math.inf
    return max((event.id for event in item_events if _closes_item(event)), default=0)


def decayed(event: Event, boundary: float) -> bool:
    """True when ``event`` is ephemeral and older than the decay boundary."""
    return is_ephemeral(event.kind) and event.id < boundary


# The roadmap importer's ticket-heading grammar (its ``_classify_heading`` and
# ``scan_roadmap``), mirrored exactly so a ticket the importer reads is one
# this view can show, and nothing else is. A heading is an ATX line ``#`` to
# ``######``, whitespace, then text (stripped) outside any code fence. A
# level-3 heading is a ticket heading in two forms:
#
# - prefixed: the text starts ``Ticket:`` (any case). After it, stripped, an
#   optional single lead word (a ``[tag]`` with no ``]`` inside, or a
#   lowercase slug such as ``bridge`` or ``ledger-repo``) and whitespace, then
#   the id, then end of text or a separator (``: `` or a spaced em dash, en
#   dash or hyphen) and a title. The id may carry no digit. A prefixed heading
#   whose tail is not that shape is still a ticket heading, with no id.
# - bare: inside a ``## Tickets`` section (the nearest heading of level two or
#   less above it is exactly ``## Tickets``), the text is the id, then end of
#   text or a separator and a title. The id needs a digit; no lead word.
#
# An id is uppercase-led segments joined by single hyphens, each segment
# optionally ending in one lowercase letter (``SL12a``, ``CSF3-9-H1``); an
# ``RM-`` id is reserved and is never a ticket id. Any other heading is not a
# ticket heading.
_ID_PATTERN = r"[A-Z][A-Z0-9]*[a-z]?(?:-[A-Z0-9]+[a-z]?)*"
_ROADMAP_ID = re.compile(rf"^{_ID_PATTERN}$")
_ID_SEPARATOR = r"(?::\s+|\s+[\u2014\u2013-]\s+)"
_LEAD_WORD = r"(?:\[[^\]]+\]|[a-z]+(?:-[a-z]+)*)\s+"
_BARE_FORM = re.compile(rf"^(?P<id>{_ID_PATTERN})(?:{_ID_SEPARATOR}(?P<title>\S.*))?$")
_LEAD_FORM = re.compile(
    rf"^(?P<lead>{_LEAD_WORD})?(?P<id>{_ID_PATTERN})(?:{_ID_SEPARATOR}(?P<title>\S.*))?$"
)
_TICKET_PREFIX = re.compile(r"^ticket:(?P<tail>.*)$", re.IGNORECASE)
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TICKETS_SECTION = "Tickets"
# Mirrors search.py ``_FENCE``: up to three spaces of indent, a run of three
# or more backticks or tildes, then the rest of the line (an info string on
# an opener; a closer must have nothing but whitespace there). The indent is
# literal spaces only, as in CommonMark and the importer: a tab or a
# non-breaking space before the run means the line is not a fence.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _newline_lines(text: str) -> list[str]:
    """Split ``text`` into lines on the newline character alone, keeping each
    line's end, so line N is the Nth line the importer counts.

    ``str.splitlines`` also breaks on form feed, vertical tab, U+001C to
    U+001E, U+0085, U+2028 and U+2029; one of those above a ticket would
    put the importer's recorded line on another row. A final line without a
    newline is the last line, as before."""
    lines = text.split("\n")
    tail = lines.pop()
    out = [line + "\n" for line in lines]
    if tail:
        out.append(tail)
    return out


def _opens_fence(run: str, rest: str) -> bool:
    """Whether a fence-shaped line opens a fence. Per CommonMark, a backtick
    fence's info string holds no backtick: such a line is ordinary text and
    opens nothing. A tilde opener may carry a backtick."""
    return not (run[0] == "`" and "`" in rest)


def _accept_ticket_id(value: str, *, prefixed: bool) -> bool:
    """Whether ``value`` is a ticket id: the id pattern, not ``RM-``, and,
    for a bare heading, at least one digit."""
    if _ROADMAP_ID.fullmatch(value) is None or value.startswith("RM-"):
        return False
    return prefixed or any(char.isdigit() for char in value)


def ticket_heading(text: str, *, in_tickets: bool) -> tuple[bool, str]:
    """Classify the text of a level-3 heading as the importer does.

    Returns ``(is_ticket, ticket_id)``: ``is_ticket`` is True for a prefixed
    heading of any tail and for a bare heading inside ``## Tickets`` with an
    accepted id; ``ticket_id`` is the heading's id, or ``""`` when it has none.
    """
    prefix = _TICKET_PREFIX.match(text)
    if prefix is not None:
        match = _LEAD_FORM.fullmatch(prefix.group("tail").strip())
        if match is not None and _accept_ticket_id(match.group("id"), prefixed=True):
            return True, match.group("id")
        return True, ""
    if not in_tickets:
        return False, ""
    bare = _BARE_FORM.fullmatch(text)
    if bare is not None and _accept_ticket_id(bare.group("id"), prefixed=False):
        return True, bare.group("id")
    return False, ""


def _headings(lines: Sequence[str]) -> list[tuple[int, str, bool] | None]:
    """Per line, ``(level, text, in_tickets)`` for a heading outside any code
    fence, else None. ``in_tickets`` is whether the nearest heading of level
    two or less at or above the line is exactly ``## Tickets``.

    A fence closes only on the same marker character, at least as long as the
    opening run, with nothing but whitespace after it (CommonMark); a fence
    line and every line inside a fence is never a heading.
    """
    out: list[tuple[int, str, bool] | None] = []
    open_fence = None
    inside = False
    for raw in lines:
        line = raw.split("\n", 1)[0].rstrip("\r")
        fence = _FENCE.match(line)
        if fence:
            run, rest = fence.group(1), fence.group(2)
            if open_fence is None:
                if _opens_fence(run, rest):
                    open_fence = run
            elif (
                run[0] == open_fence[0]
                and len(run) >= len(open_fence)
                and not rest.strip()
            ):
                open_fence = None
            out.append(None)
            continue
        if open_fence is not None:
            out.append(None)
            continue
        match = _ATX_HEADING.match(line)
        if match is None:
            out.append(None)
            continue
        level, heading = len(match.group(1)), match.group(2).strip()
        if level <= 2:
            inside = level == 2 and heading == _TICKETS_SECTION
        out.append((level, heading, inside))
    return out


# A recorded commit is a git object name (7 to 64 lowercase hex digits; 64
# covers SHA-256 repositories and the file digest a non-git source records),
# optionally marked ``+dirty``, or the literal ``untracked``. Anything else
# could reach ``git show`` as an option, so it is refused before git runs.
_LOCATOR_COMMIT = re.compile(r"(?:[0-9a-f]{7,64}(?:\+dirty)?|untracked)")


def _valid_locator_commit(sha: str) -> bool:
    return _LOCATOR_COMMIT.fullmatch(sha) is not None


def _valid_locator_path(rel: str) -> bool:
    if not rel or rel.startswith("/"):
        return False
    return ".." not in rel.split("/")


def valid_source_locator(source: str) -> bool:
    """True when ``source`` is a locator ``--ticket`` can read.

    The importer's write-side check is this function, so a source refused on
    read is refused on write.
    """
    try:
        split_source_locator(source)
    except ValueError:
        return False
    return True


def split_source_locator(source: str) -> tuple[str, int, str]:
    """Split a ``rel:line@sha`` locator. Raises ValueError when malformed.

    The commit must be 7 to 64 lowercase hex digits, optionally followed by
    ``+dirty``, or ``untracked``. The path must be relative with no ``..``
    segment.
    """
    rel, sep, after = source.partition(":")
    if not sep:
        raise ValueError(f"invalid source locator: {source}")
    lineno, sep, sha = after.partition("@")
    if not sep or not lineno.isdigit() or int(lineno) < 1:
        raise ValueError(f"invalid source locator: {source}")
    if not _valid_locator_commit(sha) or not _valid_locator_path(rel):
        raise ValueError(f"invalid source locator: {source}")
    return rel, int(lineno), sha


def ticket_section(text: str, line: int) -> str:
    """Return the ticket section that contains 1-indexed ``line``.

    The section starts at the nearest heading of level three or less at or
    above ``line``, which must be a ticket heading (``ticket_heading``): a
    ``### Ticket:`` heading, or a bare ``### <ID>`` heading inside a
    ``## Tickets`` section. It runs through the line before the next heading
    of level three or less. A line inside a ``` or ~~~ fence is never a
    heading, so it neither starts nor ends the section; an opening fence may
    carry an info string. Lines are numbered by newline alone, as the
    importer numbers them (``_newline_lines``).
    """
    lines = _newline_lines(text)
    if line < 1:
        raise ValueError("ticket section not found")
    headings = _headings(lines)
    start = None
    last = min(line, len(lines)) - 1
    for i in range(last, -1, -1):
        heading = headings[i]
        if heading is None or heading[0] > 3:
            continue
        level, heading_text, in_tickets = heading
        if level == 3 and ticket_heading(heading_text, in_tickets=in_tickets)[0]:
            start = i
        break
    if start is None:
        raise ValueError("ticket section not found")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        heading = headings[j]
        if heading is not None and heading[0] <= 3:
            end = j
            break
    section = "".join(lines[start:end])
    if not section.endswith("\n"):
        section += "\n"
    return section


def _git_root(start: Path) -> Path | None:
    cur = start.resolve()
    if cur.is_file():
        cur = cur.parent
    while True:
        if (cur / ".git").exists():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def read_source_blob(source: str, *, cwd: Path | None = None) -> str:
    """Read the recorded source path at its recorded commit."""
    rel, _line, sha = split_source_locator(source)
    if sha.endswith("+dirty"):
        # Recorded from uncommitted text: the commit can hold a different
        # ticket at that line, so it is never read in its place.
        raise ValueError(
            f"source recorded from uncommitted text; the committed copy may differ: {source}"
        )
    sha_clean = sha
    root = (cwd or Path.cwd()).resolve()
    repo = _git_root(root)
    if repo is not None and sha_clean != "untracked":
        try:
            proc = subprocess.run(
                # --end-of-options keeps the revision an argument even if
                # the locator check above ever lets a "-" through.
                ["git", "-C", str(repo), "show", "--end-of-options", f"{sha_clean}:{rel}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"source not found: {source}") from exc
        if proc.returncode == 0:
            return proc.stdout
    raise ValueError(f"source not found: {source}")


# The anchor locator ``<rel>#<ID>`` is what the roadmap importer and
# ``row-item.sh ensure`` write in an ``also at`` note: a repo-relative path, a
# ``#`` and a ticket id (a letter, then letters, digits, ``_`` or ``-``). It
# names no line or commit, so it is read from the committed ``HEAD`` of the
# repository the locator form uses, and the section is the one whose ticket
# heading (``ticket_heading``) carries exactly that id: a ``### Ticket:``
# heading, with or without a lead ``[tag]`` or slug, or a bare ``### <ID>``
# heading inside a ``## Tickets`` section. A bare heading outside that section,
# a fenced heading or a table row is not found here.
_TICKET_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def split_anchor_locator(locator: str) -> tuple[str, str]:
    """Split a ``rel#ID`` anchor locator. Raises ValueError when malformed."""
    rel, sep, ticket_id = locator.rpartition("#")
    if not sep or not _valid_locator_path(rel) or _TICKET_ID.fullmatch(ticket_id) is None:
        raise ValueError(f"invalid anchor locator: {locator}")
    return rel, ticket_id


def valid_anchor_locator(locator: str) -> bool:
    try:
        split_anchor_locator(locator)
    except ValueError:
        return False
    return True


def read_anchor_blob(locator: str, *, cwd: Path | None = None) -> str:
    """Read the anchor locator's path at the committed ``HEAD`` of the
    repository around ``cwd``."""
    rel, _ticket_id = split_anchor_locator(locator)
    repo = _git_root((cwd or Path.cwd()).resolve())
    if repo is not None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "show", "--end-of-options", f"HEAD:{rel}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"source not found: {locator}") from exc
        if proc.returncode == 0:
            return proc.stdout
    raise ValueError(f"source not found: {locator}")


def anchor_ticket_line(text: str, ticket_id: str) -> int | None:
    """The 1-indexed line of the one ticket heading (``ticket_heading``) in
    ``text`` whose id is ``ticket_id``, outside any code fence; None when there
    is none. More than one is a ValueError: the section is never guessed."""
    found: list[int] = []
    for number, heading in enumerate(_headings(_newline_lines(text)), start=1):
        if heading is None or heading[0] != 3:
            continue
        _level, heading_text, in_tickets = heading
        is_ticket, heading_id = ticket_heading(heading_text, in_tickets=in_tickets)
        if is_ticket and heading_id and heading_id == ticket_id:
            found.append(number)
    if len(found) > 1:
        raise ValueError(
            f"row_source_anchor_ambiguous: {len(found)} ticket headings for {ticket_id}"
        )
    return found[0] if found else None


# An item minted from a roadmap row has the row key ``row:<ID>`` as its created
# source, not a locator. Its locator is the one its newest ``also at <locator>``
# note names. The note text is exactly ``also at `` and then a locator in
# either form: ``rel:line@commit`` (``valid_source_locator``) or the anchor
# form ``rel#ID`` (``valid_anchor_locator``). No other case, no extra or
# trailing whitespace, no line break or control character, no bullet or fence.
# A note of any other shape, or of another kind, is not an also-at note and is
# skipped, never repaired. The newest note of either form wins.
ROW_SOURCE_PREFIX = "row:"
ALSO_AT_PREFIX = "also at "


def is_row_source(source: str) -> bool:
    """True for a row key ``row:<ID>``: the prefix, then a non-empty id with
    no whitespace or control character."""
    key = source[len(ROW_SOURCE_PREFIX):]
    return (
        source.startswith(ROW_SOURCE_PREFIX)
        and key != ""
        and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in key)
    )


def also_at_note_locator(text: object) -> str | None:
    """The locator in an ``also at <locator>`` note text, or None when the text
    is not exactly that form with a valid locator."""
    if not isinstance(text, str) or not text.startswith(ALSO_AT_PREFIX):
        return None
    locator = text[len(ALSO_AT_PREFIX):]
    if locator != locator.strip():
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in locator):
        return None
    if not (valid_source_locator(locator) or valid_anchor_locator(locator)):
        return None
    return locator


def newest_also_at_locator(events: Sequence[Event]) -> str | None:
    """The locator of the last valid ``also at`` note in event order (oldest
    first), or None when the item has none."""
    for event in reversed(events):
        if event.kind != "item.note":
            continue
        locator = also_at_note_locator(event.payload.get("text"))
        if locator is not None:
            return locator
    return None


class TicketSourceNamesNoFile(ValueError):
    """The item's source names no file ``--ticket`` could read: it is empty, it
    is neither a valid locator nor a row key, or it is a row key with no valid
    ``also at`` note. A source that names a file never raises this, whatever is
    wrong with the file or its anchor; those stay plain ``ValueError``."""


def item_ticket_text(lg: Ledger, item_id: str, *, cwd: Path | None = None) -> str:
    """The recorded ticket section for one imported item (``ticket_section``).

    A source that is a locator is read as it is. A source that is a row key
    ``row:<ID>`` reads the locator of the item's newest valid ``also at`` note,
    either ``rel:line@commit`` or the anchor form ``rel#ID`` (the committed
    ``HEAD`` copy of ``rel``, the section of the ticket heading for ``ID``); an item with
    none, or an anchor that is absent or repeated, is refused by name, and no
    section is guessed. A source that names no file at all raises
    ``TicketSourceNamesNoFile`` (a ``ValueError`` with the same message as ever),
    so the ``export-item`` verb can print the item's spec instead.
    """
    events = lg.events(item_id=item_id)
    created = next((event for event in events if event.kind == "item.created"), None)
    if created is None:
        raise ValueError(f"no item.created on {item_id}")
    source = created.payload.get("source") or ""
    if not source:
        raise TicketSourceNamesNoFile(f"no source locator on {item_id}")
    locator = source
    # A source that is a valid locator is always read as one, so a path that
    # happens to be named ``row`` keeps its meaning.
    if not valid_source_locator(source) and is_row_source(source):
        found = newest_also_at_locator(events)
        if found is None:
            raise TicketSourceNamesNoFile(
                f"row_source_no_also_at: {item_id} has the row source {source} "
                "and no valid 'also at <locator>' note"
            )
        locator = found
        if not valid_source_locator(locator):
            # The anchor form: no line, no commit; find the section by id.
            _rel, ticket_id = split_anchor_locator(locator)
            blob = read_anchor_blob(locator, cwd=cwd)
            line = anchor_ticket_line(blob, ticket_id)
            if line is None:
                raise ValueError(
                    f"row_source_anchor_not_found: no ticket heading for {ticket_id} "
                    f"in {locator}"
                )
            return ticket_section(blob, line)
    elif not valid_source_locator(source):
        # Neither a locator nor a row key (a follow-up's PARENT:slug source):
        # it names no file. Same message read_source_blob would have raised.
        raise TicketSourceNamesNoFile(f"invalid source locator: {source}")
    blob = read_source_blob(locator, cwd=cwd)
    _rel, line, _sha = split_source_locator(locator)
    return ticket_section(blob, line)


def item_export_counted(
    lg: Ledger, item_id: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``item_export`` plus how complete it is: the events returned, the events
    on the item, and the events decayed out of the view.

    Decayed events are the ephemeral ones before the latest close
    (``decay_boundary``), so ``events_on_item`` is always ``events_returned``
    plus ``events_decayed``, and a reader can tell whether it holds the whole
    record. Raises KeyError for an unknown item.
    """
    item = lg.item(item_id)
    if item is None:
        raise KeyError(item_id)
    events = lg.events(item_id=item_id)
    boundary = decay_boundary(events, item["status"] == "closed")
    shown = [event_record(event) for event in events if not decayed(event, boundary)]
    return shown, {
        "events_returned": len(shown),
        "events_on_item": len(events),
        "events_decayed": len(events) - len(shown),
    }


def item_export(lg: Ledger, item_id: str) -> list[dict[str, Any]]:
    """Every event of one item as a record, oldest first.

    Ephemeral events recorded before the item's latest close are left out
    (``decay_boundary``). They stay in the store and in the backup export;
    only this view of the item drops them. Raises KeyError for an unknown
    item.
    """
    return item_export_counted(lg, item_id)[0]


def load_export(export_dir: Path) -> list[Event]:
    export_dir = resolve_export_dir(export_dir)
    if not export_dir.is_dir():
        return []
    events: list[Event] = []
    for path in sorted(p for p in export_dir.iterdir() if EVENT_NAME.match(p.name)):
        match = EVENT_NAME.match(path.name)
        assert match is not None
        file_id = int(match.group(1))
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise ChainBroken(f"exported file {path.name}: {exc}") from exc
        if type(record) is not dict:
            raise ChainBroken(f"exported file {path.name}: not an object")
        try:
            event = event_from_record(record)
        except ChainBroken as exc:
            raise ChainBroken(f"exported file {path.name}: {exc}") from exc
        if event.id != file_id:
            raise ChainBroken(
                f"exported file {path.name}: id {event.id} does not match filename"
            )
        events.append(event)
    return events


def verify_export(export_dir: Path, *, system: str | None = None) -> list[Event]:
    """Refuse an export that is short, gapped, or has a broken chain.

    Completeness is anchored on the watermark: a bare hash chain cannot
    detect a lost tail file, so restore would silently replay fewer
    events than were exported. The export must hold at least every
    event up to the watermark and end at or beyond it. An export that
    holds event files but no watermark sidecar is refused outright:
    every export that wrote events also wrote a watermark, so a missing
    sidecar means the export directory is incomplete. An export of an
    empty ledger writes neither and restores as zero events.

    An export that carries more than one system claim is refused. With
    ``system``, the export must carry that system's claim: another system's
    lone claim is a ``SharedDatabaseError`` and an unclaimed export is
    refused as ``system-unclaimed``.
    """
    export_dir = resolve_export_dir(export_dir)
    events = load_export(export_dir)
    if events and not (export_dir / WATERMARK_NAME).is_file():
        raise LedgerError("export is incomplete: watermark.json is missing")
    watermark = read_watermark(export_dir)
    last_id = events[-1].id if events else 0
    if len(events) < watermark or last_id < watermark:
        raise LedgerError(
            f"export is short: {len(events)} events ending at {last_id}, "
            f"watermark {watermark}"
        )
    prev = GENESIS_HASH
    prev_id = 0
    for event in events:
        if event.id <= prev_id:
            raise ChainBroken(f"event {event.id}: ids are not strictly increasing")
        if event.prev_hash != prev:
            raise ChainBroken(f"event {event.id}: prev_hash {event.prev_hash} != {prev}")
        expected = event_digest(hash_body(event))
        if event.hash != expected:
            raise ChainBroken(f"event {event.id}: hash mismatch")
        validate_payload(event.kind, event.payload, event.payload_version)
        validate_item_id(event.kind, event.item_id, event.payload_version)
        prev = event.hash
        prev_id = event.id
    # One system claim per export, as in the database it came from.
    owner = check_claim_sequence(events, f"export {export_dir}")
    if system is not None:
        check_expected_system(owner, system, f"export {export_dir}")
    return events


def _parse_recorded_at(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def measure_lag(lg: Ledger, export_dir: Path, *, now: datetime | None = None) -> Lag:
    export_dir = resolve_export_dir(export_dir)
    watermark = read_watermark(export_dir)
    pending = lg.events(after_id=watermark)
    if not pending:
        return Lag(watermark=watermark, events=0, hours=0.0, oldest_recorded_at=None)
    clock = now if now is not None else datetime.now(UTC)
    oldest = pending[0].recorded_at
    age = clock - _parse_recorded_at(oldest)
    hours = max(age.total_seconds() / 3600.0, 0.0)
    return Lag(
        watermark=watermark,
        events=len(pending),
        hours=hours,
        oldest_recorded_at=oldest,
    )


def check_lag(
    lg: Ledger,
    export_dir: Path,
    *,
    max_events: int = DEFAULT_MAX_EVENTS,
    max_hours: float = DEFAULT_MAX_HOURS,
    now: datetime | None = None,
) -> Lag:
    if type(max_events) is not int or max_events < 0:
        raise LedgerError("--max-events must be an integer >= 0")
    if type(max_hours) is not float and type(max_hours) is not int:
        raise LedgerError("--max-hours must be a number >= 0")
    max_hours = float(max_hours)
    if max_hours < 0:
        raise LedgerError("--max-hours must be a number >= 0")
    lag = measure_lag(lg, export_dir, now=now)
    if lag.events > max_events or lag.hours > max_hours:
        raise LagExceeded(lag, max_events, max_hours)
    return lag


# ---- commit and push: the scan on the bytes that leave the machine ---------------

# Variables that would point git at another repository, index, object store,
# namespace, replace-ref base or shallow file; a ledger run from inside
# another repository's hook inherits them.
_GIT_ENV_DROP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
)
_HEX = re.compile(rb"[0-9a-f]+")
_OID_SIZE = {b"sha1": 40, b"sha256": 64}
_HEADS = b"refs/heads/"
_TREE_MODE = b"040000"
_GITLINK_MODE = b"160000"
_BLOB_MODES = frozenset({b"100644", b"100755", b"120000"})
# ``EVENT_NAME`` on the raw bytes of a path's final component, at any depth.
_EVENT_NAME_BYTES = re.compile(EVENT_NAME.pattern.encode("ascii"))


def _git_env(*, scan: bool = False, index_file: str | None = None) -> dict[str, str]:
    """The environment of every git command export runs.

    Variables that would point git at another repository, index, object
    store, namespace, replace-ref base or shallow file are removed, so git
    acts on the configured export directory; replace refs and the grafts
    file are turned off, so the scan and the push read one object graph.
    ``scan`` also turns off lazy fetching, so a partial clone's missing
    object is refused instead of fetched. ``index_file`` is set only while
    the commit step stages into its temporary index."""
    env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_DROP}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = os.devnull
    if scan:
        env["GIT_NO_LAZY_FETCH"] = "1"
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    return env


def _git_argv(export_dir: Path, args: Sequence[str]) -> list[str]:
    # GIT_GRAFT_FILE makes git print a grafts-deprecation hint on every
    # command; it would fill every error message, so it is turned off.
    return [
        "git", "-C", str(export_dir),
        "-c", "core.hooksPath=/dev/null",
        "-c", "advice.graftFileDeprecated=false",
        *args,
    ]


def _git(
    export_dir: Path,
    *args: str,
    input: bytes | None = None,
    scan: bool = False,
    index_file: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one git command in ``export_dir``, bytes in and bytes out, and
    return it whatever its exit status. Every git command the commit and
    push steps run to completion goes through here, with local hooks off
    and the ``_git_env`` environment."""
    return subprocess.run(
        _git_argv(export_dir, args),
        input=input,
        capture_output=True,
        env=_git_env(scan=scan, index_file=index_file),
    )


def _git_spawn(
    export_dir: Path, *args: str, scan: bool = False, stderr: int = subprocess.DEVNULL
) -> subprocess.Popen[bytes]:
    """Start one long-running git process (``cat-file --batch``,
    ``update-ref --stdin``) with the same arguments and environment."""
    return subprocess.Popen(
        _git_argv(export_dir, args),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        env=_git_env(scan=scan),
    )


def assert_export_repo(export_dir: Path) -> Path:
    """Refuse to run git outside the configured export directory."""
    export_dir = resolve_export_dir(export_dir)
    result = _git(export_dir, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        err = (
            (result.stderr or result.stdout).decode("utf-8", "replace").strip()
            or "not a git repository"
        )
        raise LedgerError(f"export directory is not a git repository: {export_dir}: {err}")
    toplevel = Path(os.fsdecode(result.stdout.strip())).resolve()
    if toplevel != export_dir:
        raise LedgerError(
            f"refusing to run git in {toplevel}; configured export directory is {export_dir}"
        )
    return export_dir


def escape_name(name: bytes | str) -> str:
    """``name`` as one line of text: undecodable bytes and every character
    that is not printable (a control character, a line separator) are shown
    as escapes, so a name cannot break or rewrite the line it is printed on."""
    text = name.decode("utf-8", "backslashreplace") if isinstance(name, bytes) else name
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in text
    )


def _refusal(cls: type[ExportRefusal], step: str, detail: str) -> ExportRefusal:
    return cls(f"export --{step} refuses: {cls.code}: {detail}")


def _git_output_text(output: bytes) -> str:
    """git's own output for a message, or a note that it was withheld when it
    holds a credential shape (naming the shapes only)."""
    hits = scan_credentials(output)
    if hits:
        return (
            "git output withheld: it holds a credential shape "
            f"({shape_names(hits)})"
        )
    return output.decode("utf-8", "replace").strip() or "no output"


def _git_failed(
    step: str, what: str, result: subprocess.CompletedProcess[bytes]
) -> ExportRefusal:
    return _refusal(
        ExportGitFailed,
        step,
        f"git {what} failed (exit {result.returncode}): "
        f"{_git_output_text(result.stderr or result.stdout)}",
    )


def _line(output: bytes) -> bytes:
    """One line of git output without its line end."""
    return output.removesuffix(b"\n")


def _oid(step: str, raw: bytes, size: int, what: str) -> str:
    """``raw`` as a full lowercase hex object id of ``size`` digits, else
    ``export_object_unreadable``."""
    if len(raw) != size or _HEX.fullmatch(raw) is None:
        raise _refusal(ExportObjectUnreadable, step, f"{what} is not a full object id")
    return raw.decode("ascii")


def _zero_oid(export_dir: Path, step: str) -> str:
    """The zero object id of the repository's hash (40 or 64 digits)."""
    result = _git(export_dir, "rev-parse", "--show-object-format")
    if result.returncode != 0:
        raise _git_failed(step, "rev-parse --show-object-format", result)
    size = _OID_SIZE.get(_line(result.stdout))
    if size is None:
        raise _refusal(
            ExportObjectUnreadable, step, "the repository's object format is not sha1 or sha256"
        )
    return "0" * size


def _pin_branch(export_dir: Path, step: str) -> bytes:
    """Read HEAD once: the branch it names, as bytes. A detached HEAD, or one
    naming a ref outside ``refs/heads/``, is ``export_head_detached``. The
    name is scanned right here, before anything names it: a credential shape
    is ``export_branch_credential``, and the name is never printed."""
    result = _git(export_dir, "symbolic-ref", "-q", "HEAD")
    if result.returncode == 1:
        raise _refusal(ExportHeadDetached, step, "HEAD is detached; check out a branch first")
    if result.returncode != 0:
        raise _git_failed(step, "symbolic-ref", result)
    target = _line(result.stdout)
    if not target.startswith(_HEADS) or target == _HEADS:
        raise _refusal(ExportHeadDetached, step, "HEAD does not name a branch under refs/heads/")
    branch = target[len(_HEADS):]
    hits = scan_credentials(branch)
    if hits:
        raise _refusal(
            ExportBranchCredential,
            step,
            f"the branch name holds a credential shape ({shape_names(hits)}); the name "
            "is not shown, and nothing was sent or committed",
        )
    return branch


def _resolve_branch(export_dir: Path, step: str, ref: str, size: int) -> str | None:
    """The commit the full ref ``ref`` holds, or None when it does not exist
    (an unborn branch). Anything that is not a commit is
    ``export_object_unreadable``."""
    result = _git(export_dir, "rev-parse", "--verify", "--quiet", "--end-of-options", ref)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        raise _git_failed(step, "rev-parse", result)
    sha = _oid(step, _line(result.stdout), size, "the branch's commit")
    kind = _git(export_dir, "cat-file", "-t", sha, scan=True)
    if kind.returncode != 0 or _line(kind.stdout) != b"commit":
        raise _refusal(
            ExportObjectUnreadable, step, f"the branch holds {sha}, which is not a readable commit"
        )
    return sha


def _commit_links(step: str, oid: str, raw: bytes, size: int) -> tuple[str, list[str]]:
    """The tree and parent ids a commit object records, read as git reads
    them: the first header line is the tree, and the parent lines are the
    lines right after it."""
    header = raw.split(b"\n\n", 1)[0].split(b"\n")
    if not header[0].startswith(b"tree "):
        raise _refusal(ExportObjectUnreadable, step, f"commit {oid} has no tree line")
    tree = _oid(step, header[0][len(b"tree "):], size, f"the tree of commit {oid}")
    parents = []
    for line in header[1:]:
        if not line.startswith(b"parent "):
            break
        parents.append(_oid(step, line[len(b"parent "):], size, f"a parent of commit {oid}"))
    return tree, parents


class _CatFile:
    """One ``git cat-file --batch`` process: objects read by id."""

    def __init__(self, export_dir: Path, step: str):
        self._step = step
        self._proc = _git_spawn(export_dir, "cat-file", "--batch", scan=True)

    def read(self, oid: str) -> tuple[bytes, bytes] | None:
        """``(type, bytes)`` of object ``oid``, or None when it is missing."""
        assert self._proc.stdin is not None and self._proc.stdout is not None
        try:
            self._proc.stdin.write(oid.encode("ascii") + b"\n")
            self._proc.stdin.flush()
        except BrokenPipeError:
            raise _refusal(ExportGitFailed, self._step, "git cat-file stopped") from None
        header = self._proc.stdout.readline()
        if not header.endswith(b"\n"):
            raise _refusal(ExportGitFailed, self._step, "git cat-file stopped")
        parts = header[:-1].split(b" ")
        if len(parts) != 3 or parts[0] != oid.encode("ascii") or not parts[2].isdigit():
            return None
        size = int(parts[2])
        data = self._proc.stdout.read(size)
        if len(data) != size or self._proc.stdout.read(1) != b"\n":
            raise _refusal(ExportGitFailed, self._step, "git cat-file stopped")
        return parts[1], data

    def close(self) -> None:
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
        if self._proc.stdout is not None:
            self._proc.stdout.close()
        self._proc.wait()


@dataclass
class _BlobUse:
    """Where a blob was first seen, under event names and other names."""

    commit: str | None
    path: bytes
    events: list[tuple[int, str | None, bytes]]
    plain: tuple[str | None, bytes] | None = None


class _RecordInvalid(Exception):
    def __init__(self, check: str):
        self.check = check
        super().__init__(check)


def _check_event_record(data: bytes) -> Event:
    """The event a blob at an event name holds, if it is a valid record:
    UTF-8 JSON, one object, every field of its type, exactly the canonical
    bytes ``write_event_files`` writes, its hash, and its payload and item id
    as ``verify_export`` checks them. Any failure is ``_RecordInvalid``
    naming the check, never content."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _RecordInvalid("utf-8") from None
    try:
        record = json.loads(text)
    except (ValueError, RecursionError):
        raise _RecordInvalid("json") from None
    if type(record) is not dict:
        raise _RecordInvalid("object")
    try:
        event = event_from_record(record)
    except ChainBroken as exc:
        raise _RecordInvalid(str(exc)) from None
    try:
        canonical = (canonical_dumps(event_record(event)) + "\n").encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise _RecordInvalid("canonical bytes") from None
    if data != canonical:
        raise _RecordInvalid("canonical bytes")
    if event.hash != event_digest(hash_body(event)):
        raise _RecordInvalid("hash")
    try:
        validate_payload(event.kind, event.payload, event.payload_version)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _RecordInvalid("payload") from None
    try:
        validate_item_id(event.kind, event.item_id, event.payload_version)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _RecordInvalid("item_id") from None
    return event


def _scan_plain_blob(data: bytes) -> tuple[Any, ...]:
    """The credential shapes in a blob that is not at an event name."""
    return scan_credentials(data)


class _Scanner:
    """The one scan rule of commit and push, over commit objects and tree
    entries. Each path is scanned once, and each blob is read once and
    checked once per kind of name that holds it."""

    def __init__(self, export_dir: Path, step: str, size: int):
        self._step = step
        self._size = size
        self._cat = _CatFile(export_dir, step)
        self._paths: set[bytes] = set()
        self._trees: dict[str, tuple[str | None, bytes]] = {}
        self._blobs: dict[str, _BlobUse] = {}

    def __enter__(self) -> _Scanner:
        return self

    def __exit__(self, *exc: object) -> None:
        self._cat.close()

    def _where(self, commit: str | None) -> str:
        return f"commit {commit}: " if commit is not None else ""

    def read_commit(self, oid: str) -> bytes:
        found = self._cat.read(oid)
        if found is None or found[0] != b"commit":
            raise _refusal(
                ExportObjectUnreadable, self._step, f"commit {oid} is missing or is not a commit"
            )
        return found[1]

    def commit_object(self, oid: str) -> tuple[str, list[str]]:
        """Scan commit ``oid``'s object (headers and message) and return the
        tree and parents it records."""
        raw = self.read_commit(oid)
        hits = scan_credentials(raw)
        if hits:
            raise _refusal(
                ExportFileCredential,
                self._step,
                f"commit {oid}: its commit object (headers and message) holds a "
                f"credential shape ({shape_names(hits)})",
            )
        return _commit_links(self._step, oid, raw, self._size)

    def entry(self, commit: str | None, mode: bytes, oid: str, path: bytes) -> None:
        """One tree entry: its path is scanned (once per distinct path), and
        its object is queued for ``finish``."""
        where = self._where(commit)
        if path not in self._paths:
            hits = scan_credentials(path)
            if hits:
                raise _refusal(
                    ExportFileCredential,
                    self._step,
                    f"{where}a path name holds a credential shape ({shape_names(hits)}); "
                    "the name is not shown",
                )
            self._paths.add(path)
        shown = escape_name(path)
        if mode == _GITLINK_MODE:
            raise _refusal(
                ExportObjectUnreadable,
                self._step,
                f"{where}{shown} is a submodule (gitlink); an export repository holds none",
            )
        if mode == _TREE_MODE:
            self._trees.setdefault(oid, (commit, path))
            return
        if mode not in _BLOB_MODES:
            raise _refusal(
                ExportObjectUnreadable, self._step, f"{where}{shown} has an unknown mode"
            )
        use = self._blobs.setdefault(oid, _BlobUse(commit, path, []))
        name = _EVENT_NAME_BYTES.match(path.rsplit(b"/", 1)[-1])
        if name is not None:
            use.events.append((int(name.group(1)), commit, path))
        elif use.plain is None:
            use.plain = (commit, path)

    def finish(self) -> None:
        """Read every queued object by id and apply the rule to each blob."""
        for oid, (commit, path) in self._trees.items():
            found = self._cat.read(oid)
            if found is None or found[0] != b"tree":
                raise _refusal(
                    ExportObjectUnreadable,
                    self._step,
                    f"{self._where(commit)}{escape_name(path)}: its object is missing or "
                    "is not a tree",
                )
        for oid, use in self._blobs.items():
            found = self._cat.read(oid)
            if found is None or found[0] != b"blob":
                raise _refusal(
                    ExportObjectUnreadable,
                    self._step,
                    f"{self._where(use.commit)}{escape_name(use.path)}: its object is "
                    "missing or is not a blob",
                )
            data = found[1]
            if use.events:
                self._check_event_blob(data, use.events)
            if use.plain is not None:
                hits = _scan_plain_blob(data)
                if hits:
                    commit, path = use.plain
                    raise _refusal(
                        ExportFileCredential,
                        self._step,
                        f"{self._where(commit)}{escape_name(path)} holds a credential "
                        f"shape ({shape_names(hits)})",
                    )

    def _check_event_blob(
        self, data: bytes, names: list[tuple[int, str | None, bytes]]
    ) -> None:
        _first_id, commit, path = names[0]
        try:
            event = _check_event_record(data)
        except _RecordInvalid as exc:
            raise _refusal(
                ExportEventInvalid,
                self._step,
                f"{self._where(commit)}{escape_name(path)} is not a valid event record "
                f"(failed check: {exc.check})",
            ) from None
        for name_id, commit, path in names:
            if event.id != name_id:
                raise _refusal(
                    ExportEventInvalid,
                    self._step,
                    f"{self._where(commit)}{escape_name(path)} is not a valid event record "
                    "(failed check: id)",
                )
        reason = unmarked_credential_reason(event)
        if reason is not None:
            raise _refusal(
                ExportDocCredential,
                self._step,
                f"{self._where(commit)}{escape_name(path)}: event {event.id} ({event.kind}) "
                f"on item {escape_name(str(event.item_id))}: {reason}",
            )


def _diff_tree_entries(output: bytes) -> list[tuple[str, bytes, bytes, bytes]]:
    """``(commit, mode, object, path)`` for every entry ``diff-tree --stdin
    -r -t -z`` prints that is not a deletion."""
    tokens = output.split(b"\0")
    entries = []
    commit = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token:
            continue
        if not token.startswith(b":"):
            commit = token
            continue
        fields = token[1:].split(b" ")
        if len(fields) != 5 or i >= len(tokens) or commit is None:
            raise ValueError("unexpected diff-tree output")
        path = tokens[i]
        i += 1
        if fields[4] == b"D":
            continue
        entries.append((commit.decode("ascii", "replace"), fields[1], fields[3], path))
    return entries


def _ls_tree_entries(output: bytes) -> list[tuple[bytes, bytes, bytes]]:
    """``(mode, object, path)`` for every entry ``ls-tree -r -t -z`` prints."""
    entries = []
    for record in output.split(b"\0"):
        if not record:
            continue
        header, sep, path = record.partition(b"\t")
        fields = header.split(b" ")
        if not sep or len(fields) != 3:
            raise ValueError("unexpected ls-tree output")
        entries.append((fields[0], fields[2], path))
    return entries


@dataclass(frozen=True)
class PushResult:
    sha: str
    branch: str
    remote: str | None


def _push_remote(export_dir: Path, step: str, branch: str) -> str:
    """The push remote, in git's own order, with no fallback."""
    for key in (f"branch.{branch}.pushRemote", "remote.pushDefault", f"branch.{branch}.remote"):
        result = _git(export_dir, "config", "--get", key)
        if result.returncode == 1:
            continue
        if result.returncode != 0:
            raise _git_failed(step, "config", result)
        value = _line(result.stdout)
        if not value:
            continue
        if value.startswith(b"-"):
            raise _refusal(
                ExportPushUnconfigured, step, "the configured push remote starts with a hyphen"
            )
        return os.fsdecode(value)
    raise _refusal(
        ExportPushUnconfigured,
        step,
        "no push remote is configured; set branch.<branch>.pushRemote, "
        "remote.pushDefault or branch.<branch>.remote",
    )


def _count_push_urls(export_dir: Path, step: str, remote: str) -> bool:
    """Refuse a push remote with more than one destination (a best-effort
    pre-check). True when ``remote`` names a configured remote, False when
    git takes it as a URL or path. The URLs are counted, never used or
    printed."""
    result = _git(export_dir, "remote", "get-url", "--push", "--all", "--end-of-options", remote)
    if result.returncode == 2:
        return False
    if result.returncode != 0:
        raise _git_failed(step, "remote get-url", result)
    urls = [line for line in result.stdout.split(b"\n") if line]
    if len(urls) > 1:
        raise _refusal(
            ExportPushMultipleUrls,
            step,
            f"the push remote has {len(urls)} push destinations; export --push pushes to "
            "exactly one",
        )
    if not urls:
        raise _refusal(ExportPushUnconfigured, step, "the push remote has no URL")
    return True


def _refuse_shallow(export_dir: Path, step: str) -> None:
    result = _git(export_dir, "rev-parse", "--is-shallow-repository")
    if result.returncode != 0:
        raise _git_failed(step, "rev-parse --is-shallow-repository", result)
    answer = _line(result.stdout)
    if answer == b"true":
        raise _refusal(
            ExportRepoShallow,
            step,
            "the export repository is shallow, so its history cannot be scanned in full",
        )
    if answer != b"false":
        raise _git_failed(step, "rev-parse --is-shallow-repository", result)


def _scan_history(export_dir: Path, step: str, sha: str, size: int) -> None:
    """Scan every commit, path and blob reachable from ``sha``: the whole
    history, nothing subtracted, with every parent a commit object records
    required in the listing."""
    listing = _git(export_dir, "rev-list", sha, scan=True)
    if listing.returncode != 0:
        raise _git_failed(step, "rev-list", listing)
    commits = [_oid(step, line, size, "a listed commit") for line in listing.stdout.split(b"\n") if line]
    listed = set(commits)
    with _Scanner(export_dir, step, size) as scanner:
        first_parent: dict[str, str | None] = {}
        for commit in commits:
            _tree, parents = scanner.commit_object(commit)
            for parent in parents:
                if parent not in listed:
                    raise _refusal(
                        ExportHistoryIncomplete,
                        step,
                        f"commit {commit} names parent {parent}, which the history listing "
                        "does not hold",
                    )
            first_parent[commit] = parents[0] if parents else None
        lines = "".join(
            f"{commit} {parent}\n" if parent else f"{commit}\n"
            for commit, parent in first_parent.items()
        )
        diff = _git(
            export_dir,
            "diff-tree", "--stdin", "-r", "-t", "-z", "--root", "--no-renames",
            input=lines.encode("ascii"),
            scan=True,
        )
        if diff.returncode != 0:
            raise _git_failed(step, "diff-tree", diff)
        try:
            entries = _diff_tree_entries(diff.stdout)
        except ValueError:
            raise _refusal(ExportGitFailed, step, "git diff-tree printed an unexpected listing") from None
        for commit, mode, oid, path in entries:
            scanner.entry(commit, mode, _oid(step, oid, size, "a tree entry's object"), path)
        scanner.finish()


def push_export(export_dir: Path) -> PushResult:
    """Push the pinned branch commit to the branch's push remote, after
    scanning its whole history. Separate from ``commit_export``; never opens
    the ledger.

    HEAD is read once: the branch it names and that branch's commit are
    pinned. The branch name, then every commit, path and blob reachable from
    the pinned commit, pass the scan rule before anything is sent. The one
    push sends the pinned full sha to ``refs/heads/<branch>``, with no tag,
    push option, push certificate or other ref. Every refusal is raised
    before the push command runs; ``ExportPushFailed`` means the push
    command ran and failed, so its outcome is unknown."""
    export_dir = assert_export_repo(export_dir)
    step = "push"
    branch = _pin_branch(export_dir, step)
    name = os.fsdecode(branch)
    size = len(_zero_oid(export_dir, step))
    sha = _resolve_branch(export_dir, step, f"refs/heads/{name}", size)
    if sha is None:
        raise _refusal(
            ExportPushUnborn,
            step,
            f"branch {escape_name(branch)} has no commit; there is nothing to push",
        )
    remote = _push_remote(export_dir, step, name)
    configured = _count_push_urls(export_dir, step, remote)
    _refuse_shallow(export_dir, step)
    _scan_history(export_dir, step, sha, size)
    result = _git(
        export_dir,
        "-c", "push.pushOption=",
        "push", "--no-follow-tags", "--recurse-submodules=no", "--no-signed",
        "--end-of-options",
        remote, f"{sha}:refs/heads/{name}",
    )
    if result.returncode != 0:
        raise _refusal(
            ExportPushFailed,
            step,
            f"git push of {sha} to refs/heads/{escape_name(branch)} failed after it started "
            f"(exit {result.returncode}); the outcome is unknown: the remote may or may not "
            "hold the commit, so check it (everything the push could send was scanned): "
            f"{_git_output_text(result.stderr or result.stdout)}",
        )
    return PushResult(sha=sha, branch=name, remote=remote if configured else None)


def _commit_gpg_sign(export_dir: Path, step: str) -> bool:
    """``commit.gpgSign`` as git config reads it, in git's own scope order."""
    result = _git(export_dir, "config", "--type=bool", "--get", "commit.gpgSign")
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        raise _git_failed(step, "config commit.gpgSign", result)
    value = _line(result.stdout)
    if value == b"true":
        return True
    if value == b"false":
        return False
    raise _git_failed(step, "config commit.gpgSign", result)


def _txn(proc: subprocess.Popen[bytes], command: str) -> bool:
    """Send one ``update-ref --stdin`` transaction command; True when git
    answers ``<command>: ok``."""
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(command.encode("ascii") + b"\n")
        proc.stdin.flush()
    except BrokenPipeError:
        return False
    return proc.stdout.readline() == command.encode("ascii") + b": ok\n"


def _end(proc: subprocess.Popen[bytes]) -> bytes:
    """Close the transaction process and return its error output."""
    _out, err = proc.communicate()
    return err or b""


def _swap(
    export_dir: Path, step: str, ref: str, new: str, old: str | None, zero: str
) -> None:
    """Move ``ref`` from ``old`` (the zero id when unborn) to ``new`` in one
    ``update-ref --no-deref --stdin`` transaction, committed only if HEAD
    names ``ref`` while the transaction is prepared."""
    proc = _git_spawn(
        export_dir, "update-ref", "--no-deref", "-m", "ledger export: commit", "--stdin",
        stderr=subprocess.PIPE,
    )
    head_moved = None
    try:
        prepared = _txn(proc, "start")
        if prepared:
            assert proc.stdin is not None
            try:
                proc.stdin.write(
                    b"update " + os.fsencode(ref) + b" " + new.encode("ascii") + b" "
                    + (old or zero).encode("ascii") + b"\n"
                )
            except BrokenPipeError:
                prepared = False
        prepared = prepared and _txn(proc, "prepare")
        if prepared:
            head = _git(export_dir, "symbolic-ref", "-q", "HEAD")
            if head.returncode != 0 or _line(head.stdout) != os.fsencode(ref):
                _txn(proc, "abort")
                head_moved = "HEAD no longer names the branch; nothing moved"
            elif not _txn(proc, "commit"):
                prepared = False
    finally:
        err = _end(proc)
    if head_moved is not None:
        raise _refusal(ExportHeadMoved, step, head_moved)
    if prepared:
        return
    head = _git(export_dir, "symbolic-ref", "-q", "HEAD")
    current = _git(export_dir, "rev-parse", "--verify", "--quiet", "--end-of-options", ref)
    holds = _line(current.stdout).decode("ascii", "replace") if current.returncode == 0 else None
    if holds == new:
        # The transaction's own commit reported no answer but the branch
        # holds the new commit: the swap landed.
        return
    if head.returncode != 0 or _line(head.stdout) != os.fsencode(ref) or holds != old:
        raise _refusal(
            ExportHeadMoved,
            step,
            "the branch no longer holds the commit this step read, or HEAD no longer names "
            "it; nothing moved",
        )
    raise _refusal(
        ExportGitFailed,
        step,
        f"git update-ref failed: {_git_output_text(err)}",
    )


def _refresh_index(export_dir: Path, ref: str, new: str) -> bool:
    """Set the real index to ``new``'s tree, only if HEAD still names
    ``ref`` and ``ref`` holds ``new``. Best effort: True when it ran."""
    head = _git(export_dir, "symbolic-ref", "-q", "HEAD")
    if head.returncode != 0 or _line(head.stdout) != os.fsencode(ref):
        return False
    tip = _git(export_dir, "rev-parse", "--verify", "--quiet", "--end-of-options", ref)
    if tip.returncode != 0 or _line(tip.stdout) != new.encode("ascii"):
        return False
    return _git(export_dir, "read-tree", new).returncode == 0


def _remove_private_dir(path: str) -> None:
    for entry in os.listdir(path):
        try:
            os.unlink(os.path.join(path, entry))
        except FileNotFoundError:
            pass
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass


def commit_export(export_dir: Path, message: str = "ledger export") -> CommitResult:
    """One scanned git commit in ``export_dir``. Never pushes.

    HEAD is read once and must name a branch; that branch's commit (or none,
    when unborn) is the old head. The work tree is staged into a private
    temporary index seeded from the old head, never the repository's own;
    the exact tree it writes, and then the new commit object, pass the scan
    rule; the commit's parent is the old head. Only the pinned branch moves,
    by a compare-and-swap from the old head, and only if HEAD names it when
    checked. Every refusal leaves HEAD, every ref and the real index as they
    were. After the swap the real index is set to the new tree, best effort
    (``index_refreshed``)."""
    export_dir = assert_export_repo(export_dir)
    step = "commit"
    branch = _pin_branch(export_dir, step)
    ref = f"refs/heads/{os.fsdecode(branch)}"
    zero = _zero_oid(export_dir, step)
    old = _resolve_branch(export_dir, step, ref, len(zero))
    git_dir = _git(export_dir, "rev-parse", "--absolute-git-dir")
    if git_dir.returncode != 0:
        raise _git_failed(step, "rev-parse --absolute-git-dir", git_dir)
    # The temporary index does not carry the real index's skip-worktree bits.
    private = tempfile.mkdtemp(prefix="ledger-export-index-", dir=os.fsdecode(_line(git_dir.stdout)))
    try:
        new = _commit_tree(
            export_dir, step, message, old, len(zero), os.path.join(private, "index")
        )
    finally:
        _remove_private_dir(private)
    if new is None:
        return CommitResult(sha=None, pushed=False, index_refreshed=False)
    _swap(export_dir, step, ref, new, old, zero)
    return CommitResult(sha=new, pushed=False, index_refreshed=_refresh_index(export_dir, ref, new))


def _commit_tree(
    export_dir: Path, step: str, message: str, old: str | None, size: int, index: str
) -> str | None:
    """Stage into ``index``, scan the tree it writes, and make the commit
    object; None when there is nothing to commit."""
    if old is not None:
        seeded = _git(export_dir, "read-tree", old, index_file=index)
    else:
        seeded = _git(export_dir, "read-tree", "--empty", index_file=index)
    if seeded.returncode != 0:
        raise _git_failed(step, "read-tree", seeded)
    staged = _git(export_dir, "add", "-A", "--", ".", index_file=index)
    if staged.returncode != 0:
        raise _git_failed(step, "add", staged)
    written = _git(export_dir, "write-tree", index_file=index)
    if written.returncode != 0:
        raise _git_failed(step, "write-tree", written)
    tree = _oid(step, _line(written.stdout), size, "the staged tree")
    with _Scanner(export_dir, step, size) as scanner:
        if old is not None:
            old_tree, _parents = _commit_links(step, old, scanner.read_commit(old), size)
            if tree == old_tree:
                return None
        listing = _git(export_dir, "ls-tree", "-r", "-t", "-z", "--full-tree", tree, scan=True)
        if listing.returncode != 0:
            raise _git_failed(step, "ls-tree", listing)
        try:
            entries = _ls_tree_entries(listing.stdout)
        except ValueError:
            raise _refusal(ExportGitFailed, step, "git ls-tree printed an unexpected listing") from None
        if old is None and not entries:
            return None
        for mode, oid, path in entries:
            scanner.entry(None, mode, _oid(step, oid, size, "a tree entry's object"), path)
        scanner.finish()
        sign = _commit_gpg_sign(export_dir, step)
        args = ["commit-tree"]
        if old is not None:
            args += ["-p", old]
        if sign:
            args.append("-S")
        args += ["-m", message, tree]
        made = _git(export_dir, *args)
        if made.returncode != 0:
            if sign:
                raise _refusal(
                    ExportCommitSignFailed,
                    step,
                    "commit.gpgSign is true and git commit-tree -S failed, so no commit "
                    f"was made: {_git_output_text(made.stderr or made.stdout)}",
                )
            raise _git_failed(step, "commit-tree", made)
        new = _oid(step, _line(made.stdout), size, "the new commit")
        new_tree, parents = scanner.commit_object(new)
        if new_tree != tree or parents != ([old] if old is not None else []):
            raise _refusal(
                ExportCommitAncestry,
                step,
                f"the new commit {new} does not hold exactly the scanned tree with the "
                "old head as its only parent; nothing moved",
            )
    return new
