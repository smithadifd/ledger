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
size and no bytes, so it is not scanned, and export never opens a document
file. ``commit_export`` and ``push_export`` do not run the scan: they
commit or push whatever the export directory and its history hold.

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


class ExportDocCredential(LedgerError):
    """A pending document holds a credential shape and carries no
    ``credential_override``: the export is refused and nothing is written."""

    code = EXPORT_DOC_CREDENTIAL


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
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
    inline (the schema refuses that shape, so it is never stored)."""
    from .restore import inline_document_bytes

    for event in events:
        if event.kind != DOC_KIND:
            continue
        if event.payload.get(DOC_CREDENTIAL_OVERRIDE) is True:
            continue
        where = f"export refuses event {event.id} ({event.kind}) on item {event.item_id}"
        if "path" in event.payload and "content_b64" not in event.payload:
            path = event.payload["path"]
            try:
                encoded = path.encode("utf-8") if type(path) is str else None
            except UnicodeEncodeError:
                encoded = None
            if encoded is None:
                raise ExportDocCredential(
                    f"{where}: {EXPORT_DOC_CREDENTIAL}: its stored path is not UTF-8 "
                    "text, so it cannot be scanned for credential shapes; nothing was "
                    "written"
                )
            path_hits = scan_credentials(encoded)
            if path_hits:
                raise ExportDocCredential(
                    f"{where}: {EXPORT_DOC_CREDENTIAL}: its stored path holds a "
                    f"credential shape ({shape_names(path_hits)} in the path) and it "
                    "carries no credential_override; the path is not shown, and "
                    "nothing was written"
                )
            continue
        try:
            data = inline_document_bytes(event.payload)
        except ValueError:
            raise ExportDocCredential(
                f"{where}: {EXPORT_DOC_CREDENTIAL}: its inline bytes do not decode, "
                "so they cannot be scanned for credential shapes; nothing was written"
            ) from None
        hits = scan_credentials(data)
        if hits:
            raise ExportDocCredential(
                f"{where}: {EXPORT_DOC_CREDENTIAL}: its bytes hold a credential shape "
                f"({shape_names(hits)}) and it carries no credential_override; "
                "nothing was written"
            )


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


def item_ticket_text(lg: Ledger, item_id: str, *, cwd: Path | None = None) -> str:
    """The recorded ticket section for one imported item (``ticket_section``).

    A source that is a locator is read as it is. A source that is a row key
    ``row:<ID>`` reads the locator of the item's newest valid ``also at`` note,
    either ``rel:line@commit`` or the anchor form ``rel#ID`` (the committed
    ``HEAD`` copy of ``rel``, the section of the ticket heading for ``ID``); an item with
    none, or an anchor that is absent or repeated, is refused by name, and no
    section is guessed.
    """
    events = lg.events(item_id=item_id)
    created = next((event for event in events if event.kind == "item.created"), None)
    if created is None:
        raise ValueError(f"no item.created on {item_id}")
    source = created.payload.get("source") or ""
    if not source:
        raise ValueError(f"no source locator on {item_id}")
    locator = source
    # A source that is a valid locator is always read as one, so a path that
    # happens to be named ``row`` keeps its meaning.
    if not valid_source_locator(source) and is_row_source(source):
        found = newest_also_at_locator(events)
        if found is None:
            raise ValueError(
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
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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


def assert_export_repo(export_dir: Path) -> Path:
    """Refuse to run git outside the configured export directory."""
    export_dir = resolve_export_dir(export_dir)
    result = subprocess.run(
        ["git", "-C", str(export_dir), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "not a git repository").strip()
        raise LedgerError(f"export directory is not a git repository: {export_dir}: {err}")
    toplevel = Path(result.stdout.strip()).resolve()
    if toplevel != export_dir:
        raise LedgerError(
            f"refusing to run git in {toplevel}; configured export directory is {export_dir}"
        )
    return export_dir


def _git(export_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(export_dir), "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "git failed").strip()
        raise LedgerError(f"git {' '.join(args)}: {err}")
    return result


def commit_export(export_dir: Path, message: str = "ledger export") -> CommitResult:
    """One git commit in ``export_dir``. Never pushes."""
    export_dir = assert_export_repo(export_dir)
    _git(export_dir, "add", "-A")
    status = _git(export_dir, "status", "--porcelain")
    if not status.stdout.strip():
        return CommitResult(sha=None, pushed=False)
    _git(export_dir, "commit", "-m", message)
    sha = _git(export_dir, "rev-parse", "HEAD").stdout.strip()
    return CommitResult(sha=sha, pushed=False)


def push_export(export_dir: Path) -> str:
    """Push the export repository. Separate from ``commit_export``."""
    export_dir = assert_export_repo(export_dir)
    _git(export_dir, "push")
    return _git(export_dir, "rev-parse", "HEAD").stdout.strip()
