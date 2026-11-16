"""Print a bounded orientation pack for an item or an initiative.

``ledger context <id> [--budget N]`` reads one item and its neighbourhood in
the link graph and prints, within a budget of N words, what a seat needs to
orient: the current spec, the decisions with their standing, the open
questions, linked documents, the last notes, and the related items. The card
stays the per-item resume view; the pack is the orientation view over an
item's neighbourhood. It names the ledger revision it was read at and is never
authority: the events are.

The target is an item id. An initiative is an item of type ``initiative``, so
it resolves the same way; its members are the items that link to it with
``link --rel item`` and show as related items, and decisions tagged
``initiative:<id>`` on any item show under its decisions (a ticket also picks
up decisions tagged ``ticket:<id>``). An id that is no item is refused with
exit 1, as ``card`` refuses it.

Layout, in this order (every section heading always prints):

    # context <id>: <title>
    type: <type> · status: <status>
    revision: <event id> <hash>    (the ledger's newest event, as the card names it)
    budget: <N> words
    orientation only; not authority

    ## Spec                        latest item.spec event
    ## Decisions                   v_decision_records rows, decision.elected
    ## Open questions              v_open_decisions rows (latest re-ask only)
    ## Linked documents            the item's doc.attached events, oldest first
    ## Last notes (K of M)         the newest NOTE_LIMIT item.note events
    ## Related items (depth 1)     items linked to the target, either way
    ## Related items (depth 2)     items linked to a depth-1 item

Every content line starts ``event=<id>``, the event it was read from. A
related item names the link event that joins it to the graph (the lowest such
id) and that link, ``<from> <rel> <to>``; when its title is clipped, the clip
names the item's ``item.created`` event, which holds the title. The links that
make the graph are
``item.linked`` events whose rel names an item (``item``, ``blocks``,
``duplicate-of``) and whose target is an existing item other than the source.
Depth 2 excludes the target and every depth-1 item, so a cycle ends there.

A linked document reads ``event=<id> <role>: <title> · sha256 <12 hex>``,
read as ``doc show`` reads the item's documents; the title is escaped as
``doc show`` escapes it (``-`` when it has none) and, as with the other
free-text rows here, clipped to 30 words with the full-text pointer
(``doc show`` does not clip), and the digest is the first 12 hex digits of
the stored sha256. With ``--json`` the section also
carries ``entries``, one ``{event_id, role, title, sha256}`` per document
with the full digest, empty when the section is collapsed.

The budget counts whitespace-separated words of the printed pack (the card's
``word_count``), headings and markers included; blank lines are free. The
header and the spec always print in full. The other sections then fill in the
order above: a section prints in full when it fits beside one line for each
section after it, and otherwise collapses to one line ``## <heading>: N more``
naming how many rows it holds. Once a section collapses, every section after it
prints at its floor (collapsed, or ``none`` when empty), so no later section
fills again. Depth 1 fills before depth 2, so depth-2 items are dropped first.
An empty section prints ``none`` and never collapses. The
pack therefore fits N words unless the header, the spec and one line per
section alone exceed it; then a single line ``over budget: ...`` follows the
spec and says by how much.

Rows within a section are ordered by event id (related items by their link
event id, then item id). Nothing of the run itself (time, host, process) is
printed, so two runs at one revision print the same bytes. When an append
lands while the pack is being read, the pack is read again; after
READ_ATTEMPTS moving reads the verb refuses with exit 5.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ledger.card import word_count
from ledger.schema import SPEC_KIND, TAG_TYPES
from ledger.store import Event, Ledger
from ledger.verbs import (
    EXIT_STALE,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    item_dict,
    open_db,
    require_item,
)
from ledger.verbs.doc import item_documents
from ledger.verbs.events import text_cell

DEFAULT_BUDGET = 600
UNIT = "words"
AUTHORITY = "orientation only; not authority"
# The newest notes the pack shows; older ones are counted in the heading.
NOTE_LIMIT = 5
# A row's free text keeps this many words, then names the event with the rest.
ROW_WORDS = 30
# item.linked rels whose target names another item: the link graph's edges.
ITEM_RELS = ("item", "blocks", "duplicate-of")
READ_ATTEMPTS = 3

# (key, heading) after the spec, in fill order.
SECTIONS = (
    ("decisions", "Decisions"),
    ("open_questions", "Open questions"),
    ("documents", "Linked documents"),
    ("notes", "Last notes"),
    ("related_1", "Related items (depth 1)"),
    ("related_2", "Related items (depth 2)"),
)


@dataclass(frozen=True)
class Section:
    key: str
    heading: str
    lines: tuple[str, ...]
    empty: str = "none"
    # The documents section's structured rows for --json; None elsewhere.
    entries: tuple[Mapping[str, Any], ...] | None = None

    @property
    def rows(self) -> int:
        return len(self.lines)

    def full(self) -> list[str]:
        return [f"## {self.heading}", *(self.lines or (self.empty,))]

    def collapsed(self) -> list[str]:
        return [f"## {self.heading}: {self.rows} more"]

    def floor(self) -> list[str]:
        """The shortest form the section may take."""
        if not self.lines:
            return self.full()
        full, collapsed = self.full(), self.collapsed()
        return full if _words(full) <= _words(collapsed) else collapsed


@dataclass(frozen=True)
class Pack:
    item_id: str
    type: str
    title: str
    status: str
    revision: int
    revision_hash: str
    spec: Section
    sections: tuple[Section, ...]


def _words(lines: list[str]) -> int:
    return word_count("\n".join(lines))


def _flat(text: Any) -> str:
    return " ".join(str(text).split())


def _clip(text: Any, event_id: int) -> str:
    words = str(text).split()
    if len(words) <= ROW_WORDS:
        return " ".join(words)
    return f"{' '.join(words[:ROW_WORDS])} … (full text in event {event_id})"


def _spec_lines(spec: Event | None) -> tuple[str, ...]:
    if spec is None:
        return ()
    payload = spec.payload
    tag = f"event={spec.id}"
    lines = [f"{tag} spec v{payload['version']}: {_flat(payload['title'])}"]
    facts = []
    if payload.get("size"):
        facts.append(f"size {payload['size']}")
    if payload.get("merge_class"):
        facts.append(f"merge class {payload['merge_class']}")
    if facts:
        lines.append(f"{tag} {' · '.join(facts)}")
    for label, field in (("|", "body_md"), ("acceptance |", "acceptance")):
        text = payload.get(field)
        if not text:
            continue
        body = [line.rstrip() for line in str(text).splitlines()]
        while body and not body[0]:
            body.pop(0)
        while body and not body[-1]:
            body.pop()
        lines.extend(f"{tag} {label} {line}".rstrip() for line in body)
    return tuple(lines)


def _decision_lines(
    lg: Ledger, item: Mapping[str, Any], events: list[Event]
) -> tuple[str, ...]:
    item_id = item["item_id"]
    records = {row["decision_event_id"]: row for row in lg.decision_records(item_id=item_id)}
    if item["type"] in TAG_TYPES:
        for row in lg.decision_records(tag=(item["type"], item_id)):
            records.setdefault(row["decision_event_id"], row)
    rows: list[tuple[int, str]] = []
    for event_id, row in records.items():
        standing = row["standing"]
        if standing == "superseded" and row.get("superseded_by"):
            standing += f" by decision event={row['superseded_by']}"
        elif row.get("standing_event_id"):
            standing += f" (standing event={row['standing_event_id']})"
        where = "" if row["item_id"] == item_id else f" (on {row['item_id']})"
        rows.append((
            int(event_id),
            f"event={event_id} {standing}: {_clip(row['effect'], event_id)} "
            f"— {_flat(row['who'])}{where}",
        ))
    for event in events:
        if event.kind == "decision.elected" and event.item_id == item_id:
            question = _clip(event.payload["question"], event.id)
            chosen = _clip(event.payload["chosen"], event.id)
            rows.append((event.id, f"event={event.id} elected: {question} -> {chosen}"))
    return tuple(line for _id, line in sorted(rows))


def _question_lines(
    lg: Ledger, item_id: str, events: list[Event]
) -> tuple[str, ...]:
    asked = {
        event.id: event.payload
        for event in events
        if event.kind == "decision.asked" and event.item_id == item_id
    }
    latest: dict[str, int] = {}
    for asked_id, payload in sorted(asked.items()):
        qid = payload.get("question_id")
        if qid:
            latest[qid] = asked_id
    rows: list[tuple[int, str]] = []
    for row in lg.open_decisions():
        if row["item_id"] != item_id:
            continue
        asked_id = int(row["asked_event_id"])
        qid = asked.get(asked_id, {}).get("question_id")
        if qid and latest.get(qid) != asked_id:
            continue
        line = f"event={asked_id} open: {_clip(row['question'], asked_id)}"
        if row.get("default"):
            line += f" (default: {_clip(row['default'], asked_id)})"
        rows.append((asked_id, line))
    return tuple(line for _id, line in sorted(rows))


def _note_section(item_id: str, events: list[Event]) -> Section:
    notes = [e for e in events if e.kind == "item.note" and e.item_id == item_id]
    shown = notes[-NOTE_LIMIT:]
    heading = "Last notes"
    if len(notes) > len(shown):
        heading += f" ({len(shown)} of {len(notes)})"
    return Section(
        "notes",
        heading,
        tuple(f"event={e.id} {_clip(e.payload['text'], e.id)}" for e in shown),
    )


def _document_section(lg: Ledger, item_id: str) -> Section:
    """The item's attached documents, oldest first, one line each."""
    lines = []
    entries = []
    for event in item_documents(lg, item_id):
        payload = event.payload
        sha256 = str(payload.get("sha256"))
        title = payload.get("title")
        lines.append(
            f"event={event.id} {payload.get('role')}: "
            f"{_clip(text_cell(title), event.id)} · sha256 {sha256[:12]}"
        )
        entries.append({
            "event_id": event.id,
            "role": payload.get("role"),
            "title": title,
            "sha256": payload.get("sha256"),
        })
    return Section("documents", "Linked documents", tuple(lines), entries=tuple(entries))


def _edges(
    events: list[Event], known: Mapping[str, Mapping[str, Any]]
) -> dict[str, list[tuple[int, str, Event]]]:
    """item -> [(link event id, neighbour, link event)], in event id order."""
    edges: dict[str, list[tuple[int, str, Event]]] = {}
    for event in events:
        if event.kind != "item.linked" or event.payload.get("rel") not in ITEM_RELS:
            continue
        source, target = event.item_id, event.payload.get("target")
        if source not in known or target not in known or source == target:
            continue
        edges.setdefault(source, []).append((event.id, target, event))
        edges.setdefault(target, []).append((event.id, source, event))
    return edges


def _nearest(
    sources: list[str],
    exclude: set[str],
    edges: Mapping[str, list[tuple[int, str, Event]]],
) -> list[tuple[int, str, Event]]:
    """Neighbours of ``sources`` outside ``exclude``, each with its lowest
    joining link, ordered by (link event id, item id)."""
    best: dict[str, tuple[int, str, Event]] = {}
    for source in sources:
        for event_id, neighbour, event in edges.get(source, ()):
            if neighbour in exclude:
                continue
            if neighbour not in best or event_id < best[neighbour][0]:
                best[neighbour] = (event_id, neighbour, event)
    return sorted(best.values(), key=lambda row: (row[0], row[1]))


def _related_line(
    row: tuple[int, str, Event], known: Mapping[str, Mapping[str, Any]]
) -> str:
    event_id, item_id, event = row
    item = known[item_id]
    link = f"{event.item_id} {event.payload['rel']} {event.payload['target']}"
    return (
        f"event={event_id} {item_id} ({item['type']}, {item['status']}): "
        f"{_clip(item['title'], item['created_event_id'])} · link: {link}"
    )


def project(lg: Ledger, item_id: str) -> Pack:
    """Read the pack's content at one revision (unbounded)."""
    item = require_item(lg, item_id)
    events = lg.events()
    tail = events[-1]
    revision = tail.id
    known = {row["item_id"]: row for row in lg.items()}
    # The target as read after the events: an append between the lookup above
    # and the events read is in the revision, so the header must see it too.
    # The moving check below covers this read like every other view read.
    item = item_dict(known[item_id])
    spec = None
    for event in events:
        if event.kind == SPEC_KIND and event.item_id == item_id:
            spec = event
    edges = _edges(events, known)
    depth1 = _nearest([item_id], {item_id}, edges)
    near = {item_id} | {row[1] for row in depth1}
    depth2 = _nearest([row[1] for row in depth1], near, edges)
    sections = (
        Section("decisions", "Decisions", _decision_lines(lg, item, events)),
        Section("open_questions", "Open questions", _question_lines(lg, item_id, events)),
        _document_section(lg, item_id),
        _note_section(item_id, events),
        Section("related_1", "Related items (depth 1)",
                tuple(_related_line(row, known) for row in depth1)),
        Section("related_2", "Related items (depth 2)",
                tuple(_related_line(row, known) for row in depth2)),
    )
    if lg.events(after_id=revision, limit=1):
        raise _Moved(revision)
    return Pack(
        item_id=item_id,
        type=item["type"],
        title=_flat(item["title"]),
        status=item["status"],
        revision=revision,
        revision_hash=tail.hash,
        spec=Section("spec", "Spec", _spec_lines(spec)),
        sections=sections,
    )


class _Moved(Exception):
    """An append landed while the pack was being read."""


def header_lines(pack: Pack, budget: int) -> list[str]:
    return [
        f"# context {pack.item_id}: {pack.title}",
        f"type: {pack.type} · status: {pack.status}",
        f"revision: {pack.revision} {pack.revision_hash}",
        f"budget: {budget} {UNIT}",
        AUTHORITY,
    ]


def fit(pack: Pack, budget: int) -> tuple[str, dict[str, Any]]:
    """Render the pack within ``budget`` words; return the text and its data."""
    head = header_lines(pack, budget)
    spec = pack.spec.full()
    used = _words(head) + _words(spec)
    floors = [_words(section.floor()) for section in pack.sections]
    need = used + sum(floors)
    blocks = [head, spec]
    over = need > budget
    if over:
        blocks.append([
            f"over budget: the header, the spec and one line per section "
            f"need {need} {UNIT} of {budget}"
        ])
    shown: list[dict[str, Any]] = []
    # Once a section collapses, every later one prints at its floor, so a
    # lower-priority section never fills after a higher one was cut.
    at_floor = False
    for index, section in enumerate(pack.sections):
        full = section.full()
        cost = _words(full)
        fits = not section.lines or cost <= _words(section.collapsed())
        if not fits and not at_floor:
            fits = used + cost + sum(floors[index + 1:]) <= budget
        if fits:
            chosen, collapsed = full, False
        else:
            chosen, collapsed = section.collapsed(), True
            at_floor = True
        used += _words(chosen)
        blocks.append(chosen)
        row: dict[str, Any] = {
            "name": section.key,
            "heading": section.heading,
            "rows": section.rows,
            "collapsed": collapsed,
            "lines": [] if collapsed else list(section.lines),
        }
        if section.entries is not None:
            row["entries"] = [] if collapsed else [dict(e) for e in section.entries]
        shown.append(row)
    text = "\n\n".join("\n".join(block) for block in blocks) + "\n"
    data = {
        "item_id": pack.item_id,
        "type": pack.type,
        "title": pack.title,
        "status": pack.status,
        "revision": {"id": pack.revision, "hash": pack.revision_hash},
        "budget": budget,
        "unit": UNIT,
        "words": word_count(text),
        "over_budget": over,
        "authority": AUTHORITY,
        "spec": list(pack.spec.lines),
        "sections": shown,
    }
    return text, data


def read_pack(lg: Ledger, item_id: str) -> Pack:
    """``project`` at one revision, read again while appends land."""
    for _attempt in range(READ_ATTEMPTS):
        try:
            return project(lg, item_id)
        except _Moved:
            continue
    raise CliError(
        f"the ledger kept moving while the context for {item_id} was read; run it again",
        code="moving",
        exit_code=EXIT_STALE,
    )


def add_arguments(parser) -> None:
    parser.add_argument("item", help="an item id (an initiative is an item)")
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        metavar="N",
        help=f"at most N words, header and spec always printed (default {DEFAULT_BUDGET})",
    )


def run(args, ctx: Context) -> Result:
    if type(args.budget) is not int or args.budget < 1:
        raise UsageError("--budget must be an integer >= 1")
    lg = open_db(ctx, write=False)
    try:
        pack = read_pack(lg, args.item)
    finally:
        lg.close()
    text, data = fit(pack, args.budget)
    return Result(data=data, text=text)


VERB = Verb(
    name="context",
    help="print a bounded orientation pack for an item or initiative",
    add_arguments=add_arguments,
    run=run,
)
