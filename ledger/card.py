"""Resume card projection.

A card is a bounded, disposable view of one item. It is never authority.
The ledger events are the source of truth; a card names the ledger revision
and the event ids it was projected from so a stale copy is detectable.

Format (stable — a later reader should parse this layout, not invent one):

    # <item_id>
    revision: <event_id> <hash>
    source_event_ids: <id>, <id>, ...

    type: ...
    title: ...
    status: ...
    owner: ...
    claim: ...
    spec v<N>                  (or "spec -" when the item has no spec)
    spec moved: v<N> → v<M>    (only when the claim built against an older spec)

    ## Decisions
    ## Open questions
    ## Next step
    ## Links
    ## Children    (only when the item has children; the header shows closed/total)
    ## Blockers
    ## Notes
    ## Decision records        (these eight only when they have content)
    ## Tried
    ## Progress
    ## Review rounds
    ## Stop point
    ## Next seat skills
    ## References
    ## Conversation pointers
    ## Truncated    (only when a section was dropped)

    This card is a projection; the events are the authority (ledger export-item -- <item>)

The spec line names the item's current spec version (its latest item.spec
event, also named in source_event_ids). The moved line appears only while
the item is claimed and its claim stamped spec_version N older than the
current M. A claim rewritten by the same holder without a stamp (the loop's
hand-off and re-stamp writes) keeps the version its run of claims stamped.
``ledger spec show`` prints the spec itself; the card names it and stays
bounded.

Links lists the item's own links, then a "duplicated-by: <item>" row for
each item linked to this one as its duplicate (the duplicate's own card lists
"duplicate-of: <this item>").

An anchor's card has a Children section after Links: one line per child
(id, title, status, holder), a child that blocks another first, then in
creation order, under a header carrying closed/total over all children. An
item with no children has no section. The rollup stays whole when long
sections are capped. A capped Children section ends "(+K cut; ledger query
--parent=<item> --limit=<total>)": the children are other items' events, so
the anchor's own export does not list them, and that query prints them all.

The resume sections come from decision.recorded (with its latest standing
and result), item.tried, item.progress, review.recorded and the latest
item.stopped stamp, which names the skills the next seat invokes and the
files, PRs or events to read, by path or id. Conversation pointers come from
a sidecar the caller passes in (``pointers``); with none installed the
section is absent. Ephemeral events (progress) recorded before the item's
latest close decay out of the card. Long texts in these rows are clipped to a few dozen
words, marked "… (full text in event <id>)".

Each open question lists the ask's own fields under its question line, so
a reader holding only the card can answer it:

    - event=<id>: <question>
      ask: <ask text>
      options:
        A) <label> — <description>
        B) <label>
      recommendation: ...
      default: ...
      effort: ...
      added: ...
      unblocks: ...

Absent fields are left out. When the only option is the default itself,
the options block is left out and the default shows once. When a re-ask
reuses a question_id, only the latest of the item's asks for it counts;
an older ask the open-decisions view still lists is not shown.

When the shown rows would name more event ids than the header holds, the
oldest rows are capped first (links, notes, decisions, then questions), so
the header always names every row the card shows. Owner, claim and title
are then trimmed to what the cap needs, never below FIELD_MIN_WORDS words,
each marked "(+K words cut; ledger export-item --json -- <item>)".

Truncation drops Links, then Notes, then shortens ask fields (ask texts,
then option descriptions, then the other fields; each down to
ASK_MIN_WORDS words, marked "… (+K words cut; full text in event <id>; ledger
export-item <item> --json)"),
then caps older Decisions, then Blockers, then Open questions, and only
then trims a pathologically long title or next step, one word at a time,
from the end (marked "(+K words cut; ledger export-item --json -- <item>)"). Option labels, status and the
revision line are never trimmed. Each capped section keeps a
"(+K cut; ledger export-item --json -- <item>)" marker. The next step reads the full card, so capping never changes it;
names inside it (blockers, claimant, chosen option) are clipped to a few
words each, so its structure is never trimmed.

source_event_ids names the creation and state events, the shown rows,
and the events the next step reads. Past SOURCE_ID_LIMIT ids it keeps the
creation and state events, the newest open ask and the latest election,
then every shown row, then the newest of the rest, and ends with
"(+K cut; ledger export-item --json -- <item>)". The printed card is at most
WORD_LIMIT words.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping

from .export import decay_boundary, decayed
from .schema import SPEC_KIND
from .store import Ledger

WORD_LIMIT = 400
FOOTER = "This card is a projection; the events are the authority"
DROP_ORDER = ("links", "notes")
CAP_ORDER = ("decisions", "blockers", "open_questions")
# Rows that show an event id, capped oldest first when the header ids
# would not otherwise fit.
ID_CAP_ORDER = ("links", "notes", "decisions", "open_questions")
# Ask text, then option descriptions, then the other ask fields are shortened
# before any decision or question is capped, but none below this many words,
# so each stays recognisable.
ASK_MIN_WORDS = 10
# Ask fields shown after the options, in this order.
ASK_FIELDS = ("recommendation", "default", "effort", "added", "unblocks")

# A next step names at most this many blockers, then a count.
NEXT_STEP_BLOCKERS = 5
# Inside the next step, a blocker or claimant name keeps at most this many
# words, and the chosen option at most STEP_CHOSEN_WORDS, each then "…".
STEP_NAME_WORDS = 8
STEP_CHOSEN_WORDS = 20
# Owner and claim are trimmed like other fields, never below this many words.
FIELD_MIN_WORDS = 10
# The header names at most this many source event ids, then a count.
SOURCE_ID_LIMIT = 40

NO_ASK_RECORDED = "No open ask or election is recorded for this decision."
HELD_DECISION_STEP = (
    "Held: no open ask or election is recorded for this decision; "
    "nothing to answer until its ask is reopened."
)
UNASKED_DECISION_STEP = (
    "No open ask or election is recorded for this decision; "
    "record the ask before it can be answered."
)

# Header, then these sections in this order.
SECTIONS = (
    "decisions",
    "open_questions",
    "next_step",
    "links",
    "blockers",
    "notes",
)
# Resume sections after Notes, each shown only when it has content.
RESUME_SECTIONS = (
    "decision_records",
    "tried",
    "progress",
    "reviews",
    "stop",
    "skills",
    "refs",
    "pointers",
)
# Dropped whole before links and notes, in this order.
RESUME_DROP_ORDER = ("progress", "pointers", "tried", "reviews")
# Capped oldest first before links and notes when the header ids would not fit.
RESUME_ID_CAP_ORDER = ("progress", "tried", "reviews", "decision_records")
# A resume row's long text keeps this many words; a review shows this many
# findings; a sidecar adds at most this many pointers.
ROW_WORDS = 30
REF_WORDS = 12
REVIEW_FINDINGS = 5
POINTER_LIMIT = 10

# A sidecar returns pointers into past conversations for an item: mappings
# with "ref" (where to look) and an optional "note".
PointerSource = Callable[[str], Iterable[Mapping[str, Any]]]


def word_count(text: str) -> int:
    return len(text.split())


def _clip(text: str, limit: int) -> str:
    """At most ``limit`` words of ``text``, then "…" when any were cut."""
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + " …"


def derive_next_step(
    *,
    status: str,
    blockers: tuple[str, ...],
    open_questions: tuple[Mapping[str, Any], ...],
    owner: str | None,
    claim: str | None,
    item_type: str = "",
    chosen: str | None = None,
    item_id: str = "",
) -> str:
    """Project a next step from current status, blockers, and open questions.

    Callers pass the uncapped card's blockers and questions, so capping a
    section never changes the step. Named blockers come first (at most
    NEXT_STEP_BLOCKERS of them, then a count), then open questions; an item
    with both names both. A blocked item with an open ask and no named
    blocker is waiting on that answer. A decision item with no open ask says
    what is true instead of inviting a pickup: it names the latest
    election's ``chosen`` option, or says no ask or election is recorded.
    A claim is never hidden.

    Every name inside the step (a blocker, the claimant, the chosen option)
    is clipped to a fixed number of words, so the step stays bounded and is
    never trimmed in a way that loses its structure: the omitted-blocker
    count and "; then answer open questions" always survive.
    """
    if status == "closed":
        return "None — item is closed."
    clear = "Clear blockers."
    if blockers:
        named = ", ".join(
            _clip(name, STEP_NAME_WORDS) for name in blockers[:NEXT_STEP_BLOCKERS]
        )
        if len(blockers) > NEXT_STEP_BLOCKERS:
            named += f" {_cut_count(len(blockers) - NEXT_STEP_BLOCKERS, item_id)}"
        clear = f"Clear blockers: {named}."
    if open_questions:
        if blockers:
            return clear[:-1] + "; then answer open questions."
        return "Answer open questions before continuing."
    if blockers and item_type != "decision":
        return clear
    claimed = None
    if status == "claimed":
        who = _clip(claim or owner or "", STEP_NAME_WORDS)
        claimed = f"Continue claimed work ({who})." if who else "Continue claimed work."
    if item_type == "decision":
        if chosen is not None:
            head = f"Decided: {_clip(chosen.strip().rstrip('.'), STEP_CHOSEN_WORDS)}."
            if status == "blocked" or blockers:
                return f"{head} {clear}"
            return f"{head} {claimed or 'Nothing left to answer.'}"
        if blockers:
            return clear
        if status == "blocked":
            return HELD_DECISION_STEP
        if claimed:
            return f"{NO_ASK_RECORDED} {claimed}"
        return UNASKED_DECISION_STEP
    if status == "blocked":
        return "Clear blockers."
    return claimed or (
        f"Resume with: {shlex.join(['ledger', 'resume', f'--item={item_id}'])}".rstrip()
    )


@dataclass(frozen=True)
class Card:
    item_id: str
    type: str
    title: str
    status: str
    owner: str | None
    claim: str | None
    revision_id: int
    revision_hash: str
    source_event_ids: tuple[int, ...]
    decisions: tuple[dict[str, Any], ...]
    open_questions: tuple[dict[str, Any], ...]
    next_step: str
    links: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]
    notes: tuple[dict[str, Any], ...]
    truncated: tuple[str, ...] = ()
    omitted: tuple[tuple[str, int], ...] = ()
    # Source ids left off the header once it reaches SOURCE_ID_LIMIT.
    source_omitted: int = 0
    decision_records: tuple[dict[str, Any], ...] = ()
    tried: tuple[dict[str, Any], ...] = ()
    progress: tuple[dict[str, Any], ...] = ()
    reviews: tuple[dict[str, Any], ...] = ()
    # The latest stop stamp, as a one-row tuple, or empty.
    stop: tuple[dict[str, Any], ...] = ()
    skills: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    pointers: tuple[dict[str, Any], ...] = ()
    # The current spec version, and the version the active claim stamped.
    spec_version: int | None = None
    spec_claim_version: int | None = None
    # The item's children (see children_of) and the rollup over all of them.
    children: tuple[dict[str, Any], ...] = ()
    children_closed: int = 0
    children_total: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Structured form of the same content the text card shows."""
        return {
            "item_id": self.item_id,
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "owner": self.owner,
            "claim": self.claim,
            "revision": {"id": self.revision_id, "hash": self.revision_hash},
            "source_event_ids": list(self.source_event_ids),
            "source_event_ids_omitted": self.source_omitted,
            "decisions": [dict(row) for row in self.decisions],
            "open_questions": [dict(row) for row in self.open_questions],
            "next_step": self.next_step,
            "links": [dict(row) for row in self.links],
            "children": [dict(row) for row in self.children],
            "children_rollup": {
                "closed": self.children_closed,
                "total": self.children_total,
            },
            "blockers": list(self.blockers),
            "notes": [dict(row) for row in self.notes],
            "decision_records": [dict(row) for row in self.decision_records],
            "tried": [dict(row) for row in self.tried],
            "progress": [dict(row) for row in self.progress],
            "reviews": [dict(row) for row in self.reviews],
            "stop": dict(self.stop[0]) if self.stop else None,
            "skills": list(self.skills),
            "refs": list(self.refs),
            "pointers": [dict(row) for row in self.pointers],
            "spec_version": self.spec_version,
            "spec_claim_version": self.spec_claim_version,
            "spec_moved": spec_moved_line(self.spec_claim_version, self.spec_version),
            "truncated": list(self.truncated),
            "footer": footer_line(self.item_id),
        }


def spec_moved_line(claimed: int | None, current: int | None) -> str | None:
    """``spec moved: vN → vM`` when the claim's version N is older than M."""
    if claimed is None or current is None or current <= claimed:
        return None
    return f"spec moved: v{claimed} → v{current}"


def latest_spec_event(events: Iterable[Any]) -> Any | None:
    """The latest ``item.spec`` event among ``events`` (id order), or None."""
    latest = None
    for event in events:
        if event.kind == SPEC_KIND:
            latest = event
    return latest


def claim_spec_version(events: list[Any], item: Mapping[str, Any]) -> int | None:
    """The spec version the item's active claim builds against, or None.

    ``events`` are the item's own events in id order. The active claim is
    the item's current state when it is a claim that names a holder. Walking
    back from it through the unbroken run of that holder's claims, the newest
    stamped ``spec_version`` wins: the loop's hand-off and re-stamp rewrite
    the holder's claim without one, and that rewrite is the same claim. Any
    other state, or another holder's claim, ends the run. None when no claim
    in the run stamped a version.
    """
    if item.get("status") != "claimed":
        return None
    holder = item.get("owner") or item.get("claim")
    state_id = item.get("state_event_id")
    if not holder or state_id is None:
        return None
    for event in reversed(events):
        if event.kind != "item.state" or event.id > int(state_id):
            continue
        payload = event.payload
        if payload.get("status") != "claimed":
            return None
        if (payload.get("owner") or payload.get("claim")) != holder:
            return None
        if payload.get("spec_version") is not None:
            return int(payload["spec_version"])
    return None


def item_spec_moved(lg: Ledger, item_id: str) -> str | None:
    """The ``spec moved`` line for one item, or None (see ``spec_moved_line``)."""
    item = lg.item(item_id)
    if item is None:
        return None
    events = lg.events(item_id=item_id)
    spec = latest_spec_event(events)
    current = None if spec is None else spec.payload.get("version")
    return spec_moved_line(claim_spec_version(events, item), current)


def _dash(value: str | None) -> str:
    return value if value else "-"


def _fmt_list(lines: list[str]) -> str:
    return "\n".join(lines) if lines else "(none)"


def _fmt_decisions(rows: tuple[dict[str, Any], ...]) -> str:
    lines = [
        (
            f"- event={row['event_id']} quote_ref={row['quote_ref']}: "
            f"{row['question']} -> {row['chosen']}"
        )
        for row in rows
    ]
    return _fmt_list(lines)


# Markers name what was cut, where one event holds it that event's id, and
# the command that prints the item's events and so recovers it.


def recover_command(item_id: str) -> str:
    """The command that prints every event the card was cut from."""
    # Flags first, then ``--``: an id with a leading hyphen or shell
    # metacharacters stays one safe token in a copied command.
    return shlex.join(["ledger", "export-item", "--json", "--", item_id])


def footer_line(item_id: str) -> str:
    """The card's last line: a projection, with what is authoritative."""
    return f"{FOOTER} ({shlex.join(['ledger', 'export-item', '--', item_id])})"


def _cut_count(count: int, item_id: str) -> str:
    """Marker for rows or ids cut from a section or the header."""
    return f"(+{count} cut; {recover_command(item_id)})"


def children_command(item_id: str, total: int) -> str:
    """The command that prints every child of ``item_id``."""
    # The --flag=value form keeps an id with a leading hyphen one argument.
    return shlex.join(
        ["ledger", "query", f"--parent={item_id}", f"--limit={max(total, 1)}"]
    )


def _cut_words(count: int, item_id: str) -> str:
    """Marker for words trimmed from the title or next step."""
    return f"(+{count} words cut; {recover_command(item_id)})"


def _cut_marker(count: int, event_id: int, item_id: str) -> str:
    """Inline marker for words cut from an ask field held by ``event_id``."""
    return (
        f"… (+{count} words cut; full text in event {event_id}; "
        f"{recover_command(item_id)})"
    )


# Words the ask marker adds; the count, the id and the item id are one token each.
_MARKER_WORDS = len(_cut_marker(1, 1, "x").split())


def _shown(row: Mapping[str, Any], path: str, value: str, item_id: str) -> str:
    cut = int((row.get("omitted_words") or {}).get(path, 0))
    if cut <= 0:
        return value
    return f"{value} {_cut_marker(cut, int(row['asked_event_id']), item_id)}"


def _option_lines(row: Mapping[str, Any], item_id: str) -> list[str]:
    lines: list[str] = []
    for index, option in enumerate(row.get("options") or []):
        label = str(option.get("label") or "").strip()
        description = str(option.get("description") or "").strip()
        if description:
            description = _shown(row, f"options.{index}.description", description, item_id)
        letter = chr(ord("A") + index) if index < 26 else str(index + 1)
        # An option pick already labels its options with letters.
        if len(label) == 1 and "A" <= label <= "Z":
            letter, label = label, ""
        text = " — ".join(part for part in (label, description) if part)
        lines.append(f"    {letter}) {text}" if text else f"    {letter})")
    return lines


def _default_only(payload: Mapping[str, Any]) -> bool:
    """True when the single option is the default itself (an ordinary ask)."""
    options = payload.get("options") or []
    if len(options) != 1:
        return False
    option = options[0]
    default = str(payload.get("default") or "").strip()
    return (
        bool(default)
        and str(option.get("label") or "").strip() == default
        and not str(option.get("description") or "").strip()
    )


def _fmt_questions(rows: tuple[dict[str, Any], ...], item_id: str) -> str:
    lines: list[str] = []
    for row in rows:
        lines.append(f"- event={row['asked_event_id']}: {row['question']}")
        if row.get("ask"):
            lines.append(f"  ask: {_shown(row, 'ask', row['ask'], item_id)}")
        if row.get("options") and not row.get("default_only"):
            lines.append("  options:")
            lines.extend(_option_lines(row, item_id))
        for name in ASK_FIELDS:
            value = row.get(name)
            if value:
                lines.append(f"  {name}: {_shown(row, name, value, item_id)}")
    return _fmt_list(lines)


def _fmt_links(rows: tuple[dict[str, Any], ...]) -> str:
    lines = [
        f"- {row['rel']}: {row['target']} (event={row['event_id']})" for row in rows
    ]
    return _fmt_list(lines)


def _fmt_notes(rows: tuple[dict[str, Any], ...]) -> str:
    lines = [f"- event={row['event_id']}: {row['text']}" for row in rows]
    return _fmt_list(lines)


def _fmt_children(rows: tuple[dict[str, Any], ...]) -> str:
    lines = [
        f"- {row['item_id']}: {row['title']} [{row['status']}] holder: {_dash(row['holder'])}"
        for row in rows
    ]
    return _fmt_list(lines)


def _children_block(card: Card) -> list[str]:
    """The Children section, or nothing for an item with no children."""
    if card.children_total <= 0:
        return []
    body = _fmt_children(card.children)
    count = _omitted_count(card, "children")
    if count > 0:
        # The children are other items' events: name the query that lists them.
        marker = f"(+{count} cut; {children_command(card.item_id, card.children_total)})"
        body = marker if body == "(none)" else f"{body}\n{marker}"
    return [
        "",
        f"## Children ({card.children_closed}/{card.children_total})",
        body,
    ]


def _fmt_blockers(blockers: tuple[str, ...]) -> str:
    return _fmt_list([f"- {item}" for item in blockers])


def _fmt_records(rows: tuple[dict[str, Any], ...]) -> str:
    lines: list[str] = []
    for row in rows:
        standing = row["standing"]
        if standing == "superseded" and row.get("superseded_by"):
            standing += f" (by event {row['superseded_by']})"
        elif row.get("source_event_id"):
            standing += f" (source event {row['source_event_id']})"
        who = f"{row['who']} ({row['who_kind']}"
        if row.get("relayed_by"):
            who += f", relayed by {row['relayed_by']}"
        who += ")"
        lines.append(
            f"- event={row['event_id']} {standing}: {who} [{row['phase']}] "
            f"\"{row['verbatim']}\""
        )
        lines.append(f"  effect: {row['effect']}")
        tags = ", ".join(f"{tag['type']}:{tag['id']}" for tag in row["tags"])
        lines.append(f"  tags: {tags}")
        if row.get("evidence"):
            lines.append(f"  evidence: {row['evidence']}")
        if row.get("result"):
            lines.append(f"  result: {row['result']}")
    return _fmt_list(lines)


def _fmt_tried(rows: tuple[dict[str, Any], ...]) -> str:
    lines = []
    for row in rows:
        line = f"- event={row['event_id']}: {row['what']}"
        if row.get("outcome"):
            line += f" -> {row['outcome']}"
        lines.append(line)
    return _fmt_list(lines)


def _fmt_reviews(rows: tuple[dict[str, Any], ...], item_id: str) -> str:
    lines: list[str] = []
    for row in rows:
        lines.append(f"- event={row['event_id']} round {row['round']}: {row['verdict']}")
        lines.extend(f"  finding: {finding}" for finding in row["findings"])
        if row.get("findings_cut"):
            lines.append(
                f"  {_cut_count(row['findings_cut'], item_id)} findings; "
                f"full list in event {row['event_id']}"
            )
    return _fmt_list(lines)


def _fmt_stop(rows: tuple[dict[str, Any], ...]) -> str:
    return _fmt_list(
        [f"event={row['event_id']} [{row['phase']}]: {row['point']}" for row in rows]
    )


def _fmt_pointers(rows: tuple[dict[str, Any], ...]) -> str:
    lines = []
    for row in rows:
        line = f"- {row['ref']}"
        if row.get("note"):
            line += f": {row['note']}"
        lines.append(line)
    return _fmt_list(lines)


_RESUME_FORMAT = (
    ("decision_records", "Decision records", _fmt_records),
    ("tried", "Tried", _fmt_tried),
    ("progress", "Progress", _fmt_notes),
    ("reviews", "Review rounds", _fmt_reviews),
    ("stop", "Stop point", _fmt_stop),
    ("skills", "Next seat skills", _fmt_blockers),
    ("refs", "References", _fmt_blockers),
    ("pointers", "Conversation pointers", _fmt_pointers),
)


def _omitted_count(card: Card, section: str) -> int:
    for name, count in card.omitted:
        if name == section:
            return count
    return 0


def _with_marker(body: str, card: Card, section: str) -> str:
    count = _omitted_count(card, section)
    if count <= 0:
        return body
    marker = _cut_count(count, card.item_id)
    if body == "(none)":
        return marker
    return f"{body}\n{marker}"


def _with_inline_marker(text: str, card: Card, section: str) -> str:
    count = _omitted_count(card, section)
    if count <= 0:
        return text
    return f"{text} {_cut_words(count, card.item_id)}"


def format_card(card: Card) -> str:
    """Render the stable text/markdown card, including the authority footer."""
    source = ", ".join(str(i) for i in card.source_event_ids)
    if card.source_omitted > 0:
        source = f"{source} {_cut_count(card.source_omitted, card.item_id)}"
    parts = [
        f"# {card.item_id}",
        f"revision: {card.revision_id} {card.revision_hash}",
        f"source_event_ids: {source}",
        f"type: {card.type}",
        f"title: {_with_inline_marker(card.title, card, 'title')}",
        f"status: {card.status}",
        f"owner: {_with_inline_marker(_dash(card.owner), card, 'owner')}",
        f"claim: {_with_inline_marker(_dash(card.claim), card, 'claim')}",
        f"spec {_dash(None if card.spec_version is None else f'v{card.spec_version}')}",
    ]
    moved = spec_moved_line(card.spec_claim_version, card.spec_version)
    if moved:
        parts.append(moved)
    parts += [
        "",
        "## Decisions",
        _with_marker(_fmt_decisions(card.decisions), card, "decisions"),
        "",
        "## Open questions",
        _with_marker(
            _fmt_questions(card.open_questions, card.item_id), card, "open_questions"
        ),
        "",
        "## Next step",
        _with_inline_marker(card.next_step, card, "next_step"),
        "",
        "## Links",
        _with_marker(_fmt_links(card.links), card, "links"),
        *_children_block(card),
        "",
        "## Blockers",
        _with_marker(_fmt_blockers(card.blockers), card, "blockers"),
        "",
        "## Notes",
        _with_marker(_fmt_notes(card.notes), card, "notes"),
    ]
    for section, heading, fmt in _RESUME_FORMAT:
        rows = getattr(card, section)
        if not rows and _omitted_count(card, section) <= 0:
            continue
        body = fmt(rows, card.item_id) if fmt is _fmt_reviews else fmt(rows)
        parts.extend(["", f"## {heading}", _with_marker(body, card, section)])
    if card.truncated:
        parts.extend(["", "## Truncated", "dropped: " + ", ".join(card.truncated)])
    parts.extend(["", footer_line(card.item_id)])
    return "\n".join(parts) + "\n"


def _content_ids(card: Card) -> set[int]:
    ids: set[int] = set()
    for row in card.decisions:
        ids.add(int(row["event_id"]))
    for row in card.open_questions:
        ids.add(int(row["asked_event_id"]))
    for row in card.links:
        ids.add(int(row["event_id"]))
    for row in card.notes:
        ids.add(int(row["event_id"]))
    for section in ("decision_records", "tried", "progress", "reviews", "stop"):
        for row in getattr(card, section):
            ids.add(int(row["event_id"]))
    return ids


def _with_source(card: Card, anchor: set[int], required: set[int]) -> Card:
    """Name the anchor and shown content ids, bounded to SOURCE_ID_LIMIT."""
    visible = _content_ids(card)
    shown, cut = _bound_ids(set(anchor) | visible, required, visible)
    return replace(card, source_event_ids=shown, source_omitted=cut)


def _record_omission(
    truncated: list[str], omitted: dict[str, int], section: str, count: int
) -> None:
    if count <= 0:
        return
    if section not in truncated:
        truncated.append(section)
    omitted[section] = omitted.get(section, 0) + count


def _latest_choice(decisions: tuple[dict[str, Any], ...]) -> str | None:
    if not decisions:
        return None
    return max(decisions, key=lambda row: int(row["event_id"]))["chosen"]


def _next_step_ids(card: Card) -> set[int]:
    """Events the next step reads: every open ask and the latest election."""
    ids = {int(row["asked_event_id"]) for row in card.open_questions}
    if card.decisions:
        ids.add(max(int(row["event_id"]) for row in card.decisions))
    return ids


def _key_step_ids(card: Card) -> set[int]:
    """The newest open ask and the latest election: never cut from the header."""
    ids: set[int] = set()
    if card.open_questions:
        ids.add(max(int(row["asked_event_id"]) for row in card.open_questions))
    if card.decisions:
        ids.add(max(int(row["event_id"]) for row in card.decisions))
    return ids


def _bound_ids(
    ids: set[int], required: set[int], visible: set[int] = frozenset()
) -> tuple[tuple[int, ...], int]:
    """At most SOURCE_ID_LIMIT ids: ``required``, then ``visible``, then the newest.

    ``visible`` is every event id a shown row names (``event=N``). bound_card
    caps rows until required and visible ids fit, so on a bounded card the
    header always names every row it shows; within each tier the newest ids
    win if they ever do not fit.
    """
    ordered = sorted(ids)
    if len(ordered) <= SOURCE_ID_LIMIT:
        return tuple(ordered), 0
    keep: set[int] = set()
    for tier in (required, visible, ids):
        for event_id in sorted(set(tier) & set(ids), reverse=True):
            if len(keep) >= SOURCE_ID_LIMIT:
                break
            keep.add(event_id)
    return tuple(sorted(keep)), len(ordered) - len(keep)


def _shorten_paths(rows: tuple[dict[str, Any], ...]) -> list[tuple[int, str]]:
    """Shortenable ask fields, in the order they are shortened.

    Ask texts first, then option descriptions, then the other ask fields.
    Within each, the oldest question goes first: capping drops the oldest
    question first and the newest last, so the newest is shortened last.
    """
    paths = [(index, "ask") for index in range(len(rows))]
    for index, row in enumerate(rows):
        for option in range(len(row.get("options") or [])):
            paths.append((index, f"options.{option}.description"))
    for name in ASK_FIELDS:
        paths.extend((index, name) for index in range(len(rows)))
    return paths


def _get_path(row: Mapping[str, Any], path: str) -> str:
    if path.startswith("options."):
        _, option, key = path.split(".")
        return str(row["options"][int(option)].get(key) or "")
    return str(row.get(path) or "")


def _set_path(row: Mapping[str, Any], path: str, value: str) -> dict[str, Any]:
    updated = dict(row)
    if path.startswith("options."):
        _, option, key = path.split(".")
        options = [dict(item) for item in row["options"]]
        options[int(option)][key] = value
        updated["options"] = options
    else:
        updated[path] = value
    return updated


def _shorten_one(
    rows: tuple[dict[str, Any], ...],
    index: int,
    path: str,
    excess: int,
    *,
    overhead: int = 0,
) -> tuple[tuple[dict[str, Any], ...], int]:
    """Cut ``excess`` words (plus a new marker's own) from one ask field.

    ``excess`` already counts ``overhead``, the words the cut adds elsewhere
    on the card. The field keeps at least ASK_MIN_WORDS words. A cut that
    would not save more words than its marker and overhead add is skipped,
    so shortening never makes the card longer. Returns the new rows and the
    words cut.
    """
    row = rows[index]
    words = _get_path(row, path).split()
    omitted_words = dict(row.get("omitted_words") or {})
    already = int(omitted_words.get(path, 0))
    marker = 0 if already else _MARKER_WORDS
    room = len(words) - ASK_MIN_WORDS
    if room <= marker + overhead:
        return rows, 0
    cut = min(room, excess + marker)
    updated = _set_path(row, path, " ".join(words[: len(words) - cut]))
    omitted_words[path] = already + cut
    updated["omitted_words"] = omitted_words
    return rows[:index] + (updated,) + rows[index + 1 :], cut


def bound_card(card: Card) -> Card:
    """Fit the header ids, trim long fields, drop, shorten, cap, then trim.

    1. Cap the oldest visible rows (links, notes, decisions, questions) until
       the required ids plus every id a shown row names fit SOURCE_ID_LIMIT,
       so the header always names each row the card shows.
    2. Trim owner, claim and title to what the cap needs, never below
       FIELD_MIN_WORDS words, each with a "(+K words cut)" marker.
    3. Drop links, then notes; shorten ask fields; cap decisions, blockers,
       then questions; and as a last resort trim the title and next step.
    """
    # Keep naming the events the next step is derived from, even once their
    # rows are capped off the card. The header keeps them first when it has
    # to cut ids.
    base = {i for i in card.source_event_ids if i not in _content_ids(card)}
    anchor = base | _next_step_ids(card)
    required = base | _key_step_ids(card)

    def ids_over(candidate: Card) -> bool:
        return len(required | _content_ids(candidate)) > SOURCE_ID_LIMIT

    if word_count(format_card(card)) <= WORD_LIMIT and not ids_over(card):
        return card
    truncated: list[str] = list(card.truncated)
    omitted: dict[str, int] = dict(card.omitted)
    current = card
    keep_newest = {
        "decision_records": True,
        "decisions": True,
        "blockers": False,
        "children": False,
        "open_questions": True,
        "refs": False,
    }

    def apply(**changes: Any) -> Card:
        if "next_step" not in changes:
            changes["next_step"] = derive_next_step(
                status=current.status,
                # Capping hides blockers, questions and decisions, and trims
                # owner and claim; it does not clear, answer or undo them,
                # so the next step reads the full card.
                blockers=card.blockers,
                open_questions=card.open_questions,
                owner=card.owner,
                claim=card.claim,
                item_type=current.type,
                chosen=_latest_choice(card.decisions),
                item_id=current.item_id,
            )
        updated = replace(
            current,
            truncated=tuple(truncated),
            omitted=tuple((name, omitted[name]) for name in truncated),
            **changes,
        )
        return _with_source(updated, anchor, required)

    for section in RESUME_ID_CAP_ORDER + ID_CAP_ORDER:
        items = list(getattr(current, section))
        while items and ids_over(current):
            if section == "links":
                # Own links are listed before inbound duplicated-by rows, so
                # the oldest link is not always first: drop the lowest event id.
                items.remove(min(items, key=lambda row: row["event_id"]))
            else:
                items.pop(0)
            _record_omission(truncated, omitted, section, 1)
            current = apply(**{section: tuple(items)})

    for field in ("owner", "claim", "title"):
        words = (getattr(current, field) or "").split()
        if len(words) <= FIELD_MIN_WORDS:
            continue
        size = word_count(format_card(current))
        if size <= WORD_LIMIT:
            break
        saved = (list(truncated), dict(omitted))
        _record_omission(truncated, omitted, field, len(words) - FIELD_MIN_WORDS)
        trial = apply(**{field: " ".join(words[:FIELD_MIN_WORDS])})
        trial_size = word_count(format_card(trial))
        if trial_size >= size:
            # The marker would cost more than the cut saves.
            truncated[:] = saved[0]
            omitted.clear()
            omitted.update(saved[1])
            continue
        keep = min(len(words) - 1, FIELD_MIN_WORDS + max(0, WORD_LIMIT - trial_size))
        omitted[field] = saved[1].get(field, 0) + len(words) - keep
        current = apply(**{field: " ".join(words[:keep])})

    for section in RESUME_DROP_ORDER + DROP_ORDER:
        if word_count(format_card(current)) <= WORD_LIMIT:
            return current
        items = getattr(current, section)
        if not items:
            continue
        _record_omission(truncated, omitted, section, len(items))
        current = apply(**{section: ()})

    # Shorten ask fields before any decision or question is capped. Each cut
    # is exact: it removes the excess plus the words of its own new marker.
    for index, path in _shorten_paths(current.open_questions):
        if len(_get_path(current.open_questions[index], path).split()) <= ASK_MIN_WORDS:
            continue
        size = word_count(format_card(current))
        if size <= WORD_LIMIT:
            break
        # The first cut also adds "ask" to the Truncated section.
        header = 0
        if "ask" not in truncated:
            probe = replace(current, truncated=tuple(truncated) + ("ask",))
            header = word_count(format_card(probe)) - size
        rows, cut = _shorten_one(
            current.open_questions,
            index,
            path,
            size + header - WORD_LIMIT,
            overhead=header,
        )
        if cut:
            _record_omission(truncated, omitted, "ask", cut)
            current = apply(open_questions=rows)

    # The stop point and the next seat's skills are never capped; references
    # go last, the last listed first, only when nothing else is left to cut.
    for section in ("children", "decision_records") + CAP_ORDER + ("refs",):
        items = list(getattr(current, section))
        while word_count(format_card(current)) > WORD_LIMIT and items:
            if keep_newest[section]:
                items.pop(0)
            else:
                items.pop()
            _record_omission(truncated, omitted, section, 1)
            current = apply(**{section: tuple(items)})

    for field in ("title", "next_step"):
        words = getattr(current, field).split()
        while word_count(format_card(current)) > WORD_LIMIT and len(words) > 1:
            words.pop()
            _record_omission(truncated, omitted, field, 1)
            current = apply(**{field: " ".join(words)})
    return current


def _elected_row(event: Any) -> dict[str, Any]:
    payload = event.payload
    return {
        "event_id": event.id,
        "asked_event_id": payload.get("asked_event_id"),
        "quote_ref": payload["quote_ref"],
        "question": payload["question"],
        "chosen": payload["chosen"],
        "chosen_description": payload.get("chosen_description", ""),
        "rejected": list(payload.get("rejected") or []),
        "scope": payload["scope"],
    }


def _question_row(
    row: Mapping[str, Any], payloads: Mapping[int, Mapping[str, Any]]
) -> dict[str, Any]:
    asked_id = int(row["asked_event_id"])
    payload = payloads.get(asked_id, {})
    return {
        "asked_event_id": asked_id,
        "question": row["question"],
        "ask": payload.get("ask"),
        "options": [
            {
                "label": option.get("label"),
                "description": option.get("description", ""),
            }
            for option in payload.get("options") or []
        ],
        "recommendation": row["recommendation"],
        "default": row["default"],
        "effort": payload.get("effort"),
        "added": payload.get("added"),
        "unblocks": payload.get("unblocks"),
        # Decided on the full ask, before any shortening.
        "default_only": _default_only(payload),
    }


def _link_row(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "rel": event.payload["rel"],
        "target": event.payload["target"],
    }


def _projected_links(events: list[Any]) -> tuple[dict[str, Any], ...]:
    """Project a loop build completion as the item's authoritative PR link."""
    rows = [_link_row(event) for event in events if event.kind == "item.linked"]
    seen_prs = {row["target"] for row in rows if row["rel"] == "pr"}
    for event in events:
        if (
            event.kind == "loop.done"
            and event.payload.get("operation") == "build"
            and (target := event.payload.get("target"))
            and target not in seen_prs
        ):
            rows.append({"event_id": event.id, "rel": "pr", "target": target})
            seen_prs.add(target)
    return tuple(rows)


def _note_row(event: Any) -> dict[str, Any]:
    return {"event_id": event.id, "text": event.payload["text"]}


def _row_text(text: str, event_id: int, limit: int = ROW_WORDS) -> str:
    """At most ``limit`` words, then a marker naming the event with the rest."""
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return f"{' '.join(words[:limit])} … (full text in event {event_id})"


def _record_row(record: Mapping[str, Any]) -> dict[str, Any]:
    event_id = int(record["decision_event_id"])
    row = {
        "event_id": event_id,
        "who": _clip(record["who"], STEP_NAME_WORDS),
        "who_kind": record["who_kind"],
        "relayed_by": record["relayed_by"],
        "standing": record["standing"],
        "source_event_id": record["source_event_id"],
        "superseded_by": record["superseded_by"],
        "effect": _row_text(record["effect"], event_id),
        "tags": [dict(tag) for tag in record["tags"]],
        "verbatim": _row_text(record["verbatim"], event_id),
        "phase": record["phase"],
        "evidence": record["evidence"],
        "result": None,
    }
    if record["relayed_by"]:
        row["relayed_by"] = _clip(record["relayed_by"], STEP_NAME_WORDS)
    if record["result"]:
        row["result"] = _row_text(record["result"], int(record["result_event_id"]))
    return row


def _tried_row(event: Any) -> dict[str, Any]:
    payload = event.payload
    return {
        "event_id": event.id,
        "what": _row_text(payload["what"], event.id),
        "outcome": _row_text(payload["outcome"], event.id) if payload.get("outcome") else None,
    }


def _progress_row(event: Any) -> dict[str, Any]:
    return {"event_id": event.id, "text": _row_text(event.payload["text"], event.id)}


def _review_row(event: Any) -> dict[str, Any]:
    findings = list(event.payload.get("findings") or [])
    return {
        "event_id": event.id,
        "round": event.payload["round"],
        "verdict": event.payload["verdict"],
        "findings": [_row_text(text, event.id) for text in findings[:REVIEW_FINDINGS]],
        "findings_cut": max(0, len(findings) - REVIEW_FINDINGS),
    }


def _pointer_rows(pointers: PointerSource | None, item_id: str) -> tuple[dict[str, Any], ...]:
    """At most POINTER_LIMIT one-line pointers from an installed sidecar."""
    if pointers is None:
        return ()
    rows: list[dict[str, Any]] = []
    for entry in pointers(item_id):
        ref = " ".join(str(entry.get("ref") or "").split())
        if not ref:
            continue
        note = " ".join(str(entry.get("note") or "").split())
        rows.append({
            "ref": _clip(ref, REF_WORDS),
            "note": _clip(note, STEP_CHOSEN_WORDS) if note else None,
        })
        if len(rows) >= POINTER_LIMIT:
            break
    return tuple(rows)


def _resume_sections(
    lg: Ledger,
    item_id: str,
    item_events: list[Any],
    pointers: PointerSource | None,
) -> dict[str, Any]:
    stops = [event for event in item_events if event.kind == "item.stopped"]
    stop: tuple[dict[str, Any], ...] = ()
    skills: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    if stops:
        latest = stops[-1]
        stop = ({
            "event_id": latest.id,
            "phase": latest.payload["phase"],
            "point": _row_text(latest.payload["point"], latest.id, 3 * ROW_WORDS),
        },)
        skills = tuple(latest.payload.get("skills") or ())
        refs = tuple(_clip(ref, REF_WORDS) for ref in latest.payload.get("refs") or ())
    return {
        "decision_records": tuple(
            _record_row(record) for record in lg.decision_records(item_id=item_id)
        ),
        "tried": tuple(_tried_row(e) for e in item_events if e.kind == "item.tried"),
        "progress": tuple(_progress_row(e) for e in item_events if e.kind == "item.progress"),
        "reviews": tuple(_review_row(e) for e in item_events if e.kind == "review.recorded"),
        "stop": stop,
        "skills": skills,
        "refs": refs,
        "pointers": _pointer_rows(pointers, item_id),
    }


def children_of(
    rows: Iterable[Mapping[str, Any]], events: Iterable[Any], item_id: str
) -> list[dict[str, Any]]:
    """The items whose current anchor is ``item_id``, one row each.

    A child that ``blocks`` another child comes before it; otherwise creation
    order. A cycle among the blocks links falls back to creation order for
    what is left.
    """
    kids = sorted(
        (row for row in rows if row.get("parent_id") == item_id),
        key=lambda row: int(row["created_event_id"]),
    )
    ids = {row["item_id"] for row in kids}
    before: dict[str, set[str]] = {i: set() for i in ids}
    for event in events:
        if event.kind != "item.linked" or event.payload.get("rel") != "blocks":
            continue
        blocker, blocked = event.item_id, event.payload.get("target")
        if blocker in ids and blocked in ids and blocker != blocked:
            before[blocked].add(blocker)
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    left = list(kids)
    while left:
        pick = next((row for row in left if before[row["item_id"]] <= placed), left[0])
        left.remove(pick)
        placed.add(pick["item_id"])
        ordered.append(
            {
                "item_id": pick["item_id"],
                "title": pick["title"],
                "status": pick["status"],
                "holder": pick["owner"] if pick["owner"] is not None else pick["claim"],
            }
        )
    return ordered


def project_item(
    lg: Ledger, item: Mapping[str, Any], *, pointers: PointerSource | None = None
) -> Card:
    """Build an unbounded card from current views and the item's events.

    Ephemeral events recorded before the item's latest close have decayed
    and are left out. ``pointers`` is the conversation-pointer sidecar, when
    installed.
    """
    item_id = item["item_id"]
    events = lg.events()
    tail = events[-1]
    own_events = [event for event in events if event.item_id == item_id]
    boundary = decay_boundary(own_events, item["status"] == "closed")
    item_events = [event for event in own_events if not decayed(event, boundary)]
    elected = tuple(
        _elected_row(event)
        for event in item_events
        if event.kind == "decision.elected"
    )
    asked_payloads = {
        event.id: event.payload
        for event in item_events
        if event.kind == "decision.asked"
    }
    open_rows = sorted(
        (row for row in lg.open_decisions() if row["item_id"] == item_id),
        key=lambda row: int(row["asked_event_id"]),
    )
    # A re-ask with the same question_id supersedes every earlier ask, but the
    # open-decisions view can still list an earlier one as open (an election
    # naming the newest ask closes only that ask). The latest ask per
    # question_id is taken over ALL of the item's asks, open or not, and an
    # older open row for that question is dropped.
    latest_by_qid: dict[str, int] = {}
    for asked_id, payload in asked_payloads.items():
        qid = payload.get("question_id")
        if qid:
            latest_by_qid[qid] = max(asked_id, latest_by_qid.get(qid, asked_id))

    def superseded(row: Mapping[str, Any]) -> bool:
        asked_id = int(row["asked_event_id"])
        qid = asked_payloads.get(asked_id, {}).get("question_id")
        return bool(qid) and latest_by_qid[qid] != asked_id

    open_questions = tuple(
        _question_row(row, asked_payloads)
        for row in open_rows
        if not superseded(row)
    )
    # A duplicate-of link lives on the duplicate and names the item kept; the
    # kept item's card shows it too, as duplicated-by.
    links = _projected_links(item_events) + tuple(
        {"event_id": event.id, "rel": "duplicated-by", "target": event.item_id}
        for event in events
        if event.kind == "item.linked"
        and event.payload.get("rel") == "duplicate-of"
        and event.payload.get("target") == item_id
        and event.item_id != item_id
    )
    notes = tuple(
        _note_row(event) for event in item_events if event.kind == "item.note"
    )
    blocked_by: list[str] = []
    for event in item_events:
        if event.kind != "item.state":
            continue
        if "blocked_by" in event.payload:
            seen: list[str] = []
            for blocker in event.payload["blocked_by"]:
                text = str(blocker)
                if text not in seen:
                    seen.append(text)
            blocked_by = seen
        elif event.payload["status"] != "blocked":
            # A transition to a non-blocked status ends the block. Only a
            # transition back to "blocked" without a restated set may carry
            # the previous set forward.
            blocked_by = []
    # A "blocks" link lives on the blocker and names the item it blocks,
    # the same direction v_ready reads (migrations/0001_initial.sql): a
    # link {rel: "blocks", target: T} on item S blocks T.
    rows = lg.items()
    closed = {row["item_id"] for row in rows if row["status"] == "closed"}
    children = children_of(rows, events, item_id)
    for event in events:
        if event.kind != "item.linked":
            continue
        if event.payload.get("rel") != "blocks":
            continue
        if event.payload.get("target") != item_id:
            continue
        if event.item_id not in closed and event.item_id not in blocked_by:
            blocked_by.append(event.item_id)
    blockers = tuple(b for b in blocked_by if b not in closed)
    owner = item["owner"]
    claim = item["claim"]
    next_step = derive_next_step(
        status=item["status"],
        blockers=blockers,
        open_questions=open_questions,
        owner=owner,
        claim=claim,
        item_type=item["type"],
        chosen=_latest_choice(elected),
        item_id=item_id,
    )
    base: set[int] = set()
    created_id = item.get("created_event_id")
    if created_id is not None:
        base.add(int(created_id))
    state_id = item.get("state_event_id")
    if state_id is not None:
        base.add(int(state_id))
    spec = latest_spec_event(own_events)
    if spec is not None:
        base.add(spec.id)
    card = Card(
        item_id=item_id,
        type=item["type"],
        title=item["title"],
        status=item["status"],
        owner=owner,
        claim=claim,
        revision_id=tail.id,
        revision_hash=tail.hash,
        source_event_ids=(),
        decisions=elected,
        open_questions=open_questions,
        next_step=next_step,
        links=links,
        blockers=blockers,
        notes=notes,
        children=tuple(children),
        children_closed=sum(1 for row in children if row["status"] == "closed"),
        children_total=len(children),
        **_resume_sections(lg, item_id, item_events, pointers),
        spec_version=None if spec is None else spec.payload.get("version"),
        spec_claim_version=claim_spec_version(own_events, item),
    )
    # The creation, state and current spec events are always named; past SOURCE_ID_LIMIT
    # the header keeps them, the newest open ask and the latest election,
    # then the newest of the rest.
    return _with_source(card, base, base | _key_step_ids(card))


def project_card(
    lg: Ledger, item_id: str, *, pointers: PointerSource | None = None
) -> Card:
    """Project a bounded card for ``item_id``; raise KeyError if missing."""
    for row in lg.items():
        if row["item_id"] == item_id:
            return bound_card(project_item(lg, row, pointers=pointers))
    raise KeyError(item_id)


__all__ = [
    "ASK_FIELDS",
    "ASK_MIN_WORDS",
    "CAP_ORDER",
    "DROP_ORDER",
    "FOOTER",
    "footer_line",
    "recover_command",
    "RESUME_SECTIONS",
    "WORD_LIMIT",
    "Card",
    "PointerSource",
    "bound_card",
    "children_command",
    "children_of",
    "claim_spec_version",
    "derive_next_step",
    "format_card",
    "item_spec_moved",
    "latest_spec_event",
    "project_card",
    "project_item",
    "spec_moved_line",
    "word_count",
]
