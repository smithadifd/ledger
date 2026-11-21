"""No sequence of generic CLI write verbs, without `restore --accept-loop-events`, writes a reserved review record. In-process callers are trusted; the boundary defended is the CLI/process boundary.

The writer for the reserved review kinds. The store reserves them by
name: ``ledger append``, ``append-many``, the library's ``append`` and
``append_many`` and every other internal writer refuse them, and restore
takes them only with ``--accept-loop-events`` after the export's stream
digest is checked. The writers here append them through the store's
``_append_receipt_event``.

``write_receipt`` records one review round of one pull request. In one
write transaction it finds the one item whose ``pr`` link names the pull
request (``pr_item``) and refuses any other item; attaches the receipt
document (a UTF-8 JSON object of at most ``DOC_INLINE_LIMIT`` bytes) inline
on that item as a ``review-receipt`` document, through
``ledger.verbs.doc.attach_inline_in_transaction``; takes ``doc_sha256``
from the digest the ledger computed for those bytes, never from the caller;
checks that ``prior_receipt``, each ``carried_from`` and ``supersedes`` name
receipts of that item; and appends the receipt. Any refusal or error leaves
neither event. A ``review-receipt`` document alone is not a receipt: a
reader goes from a receipt's ``doc_sha256`` to its document, never from a
document to a receipt, so ``ledger doc add --role review-receipt`` writes no
receipt, and this writer reuses such a document when its bytes match and
are stored inline; one added ``--by-path`` is refused ``doc_not_inline``,
since its file can go missing while the receipt names its digest.

``write_grant_cite`` records that the past-cap review-round grant
(``grant:review-round-past-cap``) was STANDING, through
``ledger.verbs.grants.standing``, in the transaction that wrote the cite,
and names the election that decided it. It re-raises ``standing``'s refusal
otherwise. A cite grants nothing by itself; a withdrawal later leaves it in
the log, and ``standing`` then refuses.

``write_merge_intent`` and ``write_merge_done`` record a merge on a
PR-linked item without making it loop-managed. Their closed payload is
``{key, pr, head}``: a key ``merge:example/repo#12:HEAD``, a full PR URL,
and a 40-hex lowercase head, all agreeing. In one write transaction the
writer resolves the live PR links and reads the events for this item/key.
Absent -> started -> finished appends one event at each step. Retries
return the existing event (and state) without writing; an intent after done
returns finished. Done without intent refuses ``merge_intent_missing``.
After a stop following intent, the caller must reconcile the external merge
before writing done; these records do not prove that a remote merge happened.
Concurrent receipts writers serialize their state reads and writes. No loop
writer or loop-managed state is involved.

Refusals the writer makes are ``CliError`` (exit code ``EXIT_REFUSED``)
with a code: ``bad_pr_target``, ``pr_item_missing``, ``pr_item_ambiguous``,
``receipt_item_mismatch``, ``receipt_doc_sha256_given``,
``receipt_doc_not_json``, ``receipt_doc_too_large``,
``receipt_credential_shape``, ``receipt_prior_mismatch``,
``receipt_carried_mismatch``, ``receipt_supersedes_required`` and
``receipt_supersedes_mismatch``. A payload that breaks its schema is the
store's ``ValidationError``; the document step refuses ``doc_credential_shape``
for a credential shape in the bytes and ``doc_not_inline`` for an identical
document on the item stored by path.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .credscan import scan as scan_credentials
from .credscan import shape_names
from .schema import (
    DOC_INLINE_LIMIT,
    GRANT_CITE_KIND,
    MERGE_DONE_KIND,
    MERGE_INTENT_KIND,
    RECEIPT_DOC_ROLE,
    RECEIPT_KIND,
    validate_item_id,
    validate_payload,
)
from .store import Event, Ledger
from .verbs import EXIT_REFUSED, CliError
from .verbs.doc import attach_inline_in_transaction
from .verbs.grants import REVIEW_ROUND_GRANT_ID, standing

# The store appends reserved review records only for a caller holding this.
_RECEIPT_WRITER_CAPABILITY = object()
RECEIPT_DOC_MIME = "application/json"
# A pull request as the writer and pr_item take it: exactly the URL form
# ledger link accepts for a pr target.
_PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)"
)
# The short form a stored pr link may carry; a bare #N names no repository
# and matches nothing.
_PR_SHORT = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)")


def _refuse(code: str, message: str) -> CliError:
    return CliError(message, code=code, exit_code=EXIT_REFUSED)


def pr_url(repo: str, pr: int) -> str:
    """The pull request URL for ``owner/repo`` and its number."""
    return f"https://github.com/{repo}/pull/{pr}"


def _pull_request(target: Any) -> tuple[str, str, int] | None:
    """``(owner, repo, number)`` with owner and repo casefolded, for a target
    in the URL form or the ``owner/repo#N`` form; None for anything else."""
    if type(target) is not str:
        return None
    for pattern in (_PR_URL, _PR_SHORT):
        match = pattern.fullmatch(target)
        if match is not None:
            return match.group(1).casefold(), match.group(2).casefold(), int(match.group(3))
    return None


def pr_item(lg: Ledger, url: Any) -> str:
    """The one item, of any status, whose ``item.linked`` with rel ``pr``
    names the pull request ``url``.

    ``url`` must be exactly ``https://github.com/<owner>/<repo>/pull/<N>``,
    else ``bad_pr_target``. A stored target names the same pull request when
    it is in the URL form or the ``owner/repo#N`` form with the same owner
    and repo, casefolded, and the same number; a bare ``#N`` and any other
    stored target match nothing, and so does a link of another rel. No item:
    ``pr_item_missing``. Two or more: ``pr_item_ambiguous``, naming the item
    ids sorted. Reads inside the caller's transaction when there is one.
    """
    wanted = _pull_request(url) if type(url) is str and _PR_URL.fullmatch(url) else None
    if wanted is None:
        raise _refuse(
            "bad_pr_target",
            "a pull request is named by https://github.com/OWNER/REPO/pull/N",
        )
    items: set[str] = set()
    for row in lg._conn.execute(
        "SELECT item_id, payload FROM events WHERE kind = 'item.linked' ORDER BY id"
    ):
        payload = json.loads(row["payload"])
        if payload.get("rel") != "pr":
            continue
        if _pull_request(payload.get("target")) == wanted:
            items.add(row["item_id"])
    if not items:
        raise _refuse("pr_item_missing", f"no item links pull request {url} (rel pr)")
    if len(items) > 1:
        raise _refuse(
            "pr_item_ambiguous",
            f"pull request {url} is linked (rel pr) from more than one item: "
            + ", ".join(sorted(items)),
        )
    return next(iter(items))


def _require_pr_item(lg: Ledger, item_id: str, repo: str, pr: int) -> None:
    url = pr_url(repo, pr)
    owner = pr_item(lg, url)
    if owner != item_id:
        raise _refuse(
            "receipt_item_mismatch",
            f"pull request {url} is the item {owner}'s, not {item_id}'s",
        )


def _strings(value: Any) -> list[str]:
    """Every string in ``value``, keys included, at any depth."""
    found: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            found.append(current)
        elif isinstance(current, dict):
            for key, item in current.items():
                stack.append(key)
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _refuse_credentials(payload: Mapping[str, Any], item_id: str,
                        stamps: Mapping[str, str | None]) -> None:
    """Refuse ``receipt_credential_shape`` when any string of the payload,
    the item id or an attribution stamp holds a credential shape
    (``ledger.credscan``) or cannot be scanned. ``ledger export`` refuses the
    whole export on such an event, so it is never written. The message names
    the shapes, never the text."""
    texts = [*_strings(dict(payload)), item_id,
             *(value for value in stamps.values() if isinstance(value, str))]
    for text in texts:
        try:
            hits = scan_credentials(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise _refuse(
                "receipt_credential_shape",
                "a field of the event is not UTF-8 text, so it cannot be scanned for "
                "credential shapes; nothing was written",
            ) from None
        if hits:
            raise _refuse(
                "receipt_credential_shape",
                f"a field of the event holds a credential shape ({shape_names(hits)}); "
                "the text is not shown, and nothing was written",
            )


def _require_json_object(document: Any) -> None:
    """Refuse ``receipt_doc_not_json`` unless ``document`` is bytes holding
    UTF-8 JSON whose value is an object; ``receipt_doc_too_large`` when it is
    over ``DOC_INLINE_LIMIT`` bytes (checked first, before any parse)."""
    if type(document) is not bytes:
        raise _refuse("receipt_doc_not_json", "the receipt document must be bytes")
    if len(document) > DOC_INLINE_LIMIT:
        raise _refuse(
            "receipt_doc_too_large",
            f"the receipt document is {len(document)} bytes, over the "
            f"{DOC_INLINE_LIMIT}-byte inline limit",
        )

    def no_constant(name: str) -> Any:
        raise ValueError(f"{name} is not JSON")

    try:
        value = json.loads(document.decode("utf-8"), parse_constant=no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        value = None
    if type(value) is not dict:
        raise _refuse(
            "receipt_doc_not_json", "the receipt document is not a UTF-8 JSON object"
        )


def receipt_key(item_id: str, lane: str, round_: int, head: str, doc_sha256: str) -> str:
    """The idempotency key of a receipt: its item, lane, round, head and
    document digest."""
    return f"{RECEIPT_KIND}:{item_id}:{lane}:{round_}:{head}:{doc_sha256}"


def cite_key(item_id: str, repo: str, pr: int, round_: int, grant_event_id: int) -> str:
    """The idempotency key of a grant cite: its item, pull request, round
    and the election the grant stood on."""
    return f"{GRANT_CITE_KIND}:{item_id}:{repo}#{pr}:{round_}:{grant_event_id}"


def _item_receipts(lg: Ledger, item_id: str) -> dict[int, Event]:
    return {event.id: event for event in lg.events(item_id=item_id, kind=RECEIPT_KIND)}


def _check_links(lg: Ledger, item_id: str, payload: Mapping[str, Any]) -> None:
    """``prior_receipt``, each ``carried_from`` and ``supersedes`` against the
    receipts already on ``item_id``."""
    receipts = _item_receipts(lg, item_id)
    repo, pr, lane, round_ = payload["repo"], payload["pr"], payload["lane"], payload["round"]
    if "prior_receipt" in payload:
        prior = receipts.get(payload["prior_receipt"])
        if prior is None or (
            prior.payload["repo"], prior.payload["pr"], prior.payload["lane"],
            prior.payload["round"],
        ) != (repo, pr, lane, round_ - 1):
            raise _refuse(
                "receipt_prior_mismatch",
                f"prior_receipt {payload['prior_receipt']} is not a review.receipt on "
                f"{item_id} for {repo}#{pr}, lane {lane}, round {round_ - 1}",
            )
    for index, finding in enumerate(payload.get("findings", ())):
        if "carried_from" not in finding:
            continue
        carried = receipts.get(finding["carried_from"])
        if carried is None or (carried.payload["repo"], carried.payload["pr"]) != (repo, pr):
            raise _refuse(
                "receipt_carried_mismatch",
                f"findings[{index}].carried_from {finding['carried_from']} is not a "
                f"review.receipt on {item_id} for {repo}#{pr}",
            )
    same = [
        event for event in receipts.values()
        if (event.payload["repo"], event.payload["pr"], event.payload["lane"],
            event.payload["round"], event.payload["head"])
        == (repo, pr, lane, round_, payload["head"])
    ]
    if same:
        newest = max(event.id for event in same)
        if "supersedes" not in payload:
            raise _refuse(
                "receipt_supersedes_required",
                f"receipt {newest} already records {repo}#{pr}, lane {lane}, round "
                f"{round_} at this head; pass supersedes {newest}",
            )
        if payload["supersedes"] != newest:
            raise _refuse(
                "receipt_supersedes_mismatch",
                f"supersedes {payload['supersedes']} is not the newest receipt for "
                f"{repo}#{pr}, lane {lane}, round {round_} at this head ({newest})",
            )
    elif "supersedes" in payload:
        raise _refuse(
            "receipt_supersedes_mismatch",
            f"supersedes {payload['supersedes']}: no receipt on {item_id} records "
            f"{repo}#{pr}, lane {lane}, round {round_} at this head",
        )


def write_receipt(
    lg: Ledger,
    item_id: str,
    fields: Mapping[str, Any],
    document: bytes,
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    """Write one ``review.receipt`` on ``item_id`` with its document.

    ``fields`` is the receipt payload without ``doc_sha256`` (passing one is
    ``receipt_doc_sha256_given``); ``document`` is the receipt document's
    bytes. Before the transaction: the document's size and shape, the
    payload's schema (``ValidationError``) and a credential scan of every
    string in it, the item id and the attribution stamps. Then in ONE write
    transaction: the item is ``pr_item``'s for the payload's pull request
    (else ``receipt_item_mismatch`` or ``pr_item``'s refusal); the document is
    attached (or an identical inline ``review-receipt`` document on the item
    is reused; one stored by path is ``doc_not_inline``) and ``doc_sha256``
    is the digest the ledger computed for it; an
    exact retry (the same idempotency key) returns the stored event and
    writes nothing; otherwise ``prior_receipt``, ``carried_from`` and the
    supersedes chain are checked and the receipt is appended. A refusal or
    error at any step rolls back both events. Returns the receipt event.
    """
    stamps = {"actor": actor, "harness": harness, "model": model, "session": session}
    payload = dict(fields)
    if "doc_sha256" in payload:
        raise _refuse(
            "receipt_doc_sha256_given",
            "doc_sha256 is computed by the ledger from the document bytes; do not pass it",
        )
    _require_json_object(document)
    payload["doc_sha256"] = hashlib.sha256(document).hexdigest()
    validate_payload(RECEIPT_KIND, payload)
    validate_item_id(RECEIPT_KIND, item_id)
    _refuse_credentials(payload, item_id, stamps)
    with lg.transaction():
        _require_pr_item(lg, item_id, payload["repo"], payload["pr"])
        document_event, _created = attach_inline_in_transaction(
            lg, item_id, document, role=RECEIPT_DOC_ROLE, mime=RECEIPT_DOC_MIME, **stamps
        )
        payload["doc_sha256"] = document_event.payload["sha256"]
        key = receipt_key(
            item_id, payload["lane"], payload["round"], payload["head"], payload["doc_sha256"]
        )
        # An exact retry is recognised before the link rules: the receipt it
        # stored would otherwise demand that the retry supersede it.
        if lg.event_by_key(key) is None:
            _check_links(lg, item_id, payload)
        event = lg._append_receipt_event(
            _RECEIPT_WRITER_CAPABILITY,
            kind=RECEIPT_KIND,
            payload=payload,
            item_id=item_id,
            idempotency_key=key,
            **stamps,
        )
    return event


def write_grant_cite(
    lg: Ledger,
    item_id: str,
    *,
    repo: str,
    pr: int,
    round: int,  # noqa: A002 - the payload field's name
    scope: str,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    """Write one ``review.grant-cite`` on ``item_id``: the past-cap review
    round grant stood for round ``round`` of ``repo``'s pull request ``pr``.

    The writer sets ``grant_id`` (``grant:review-round-past-cap``) and
    ``grant_event_id`` (the election ``standing`` returns) itself; the caller
    supplies neither. In ONE write transaction the item must be ``pr_item``'s
    for the pull request (else ``receipt_item_mismatch`` or ``pr_item``'s
    refusal) and ``standing(lg, REVIEW_ROUND_GRANT_ID)`` must return; its
    refusal (``grant_unknown``, ``grant_withdrawn``, ``grant_unverified``) is
    raised as it is and nothing is written. An exact retry while the grant
    stands on the same election returns the stored event and writes
    nothing. Returns the cite event.
    """
    stamps = {"actor": actor, "harness": harness, "model": model, "session": session}
    payload: dict[str, Any] = {
        "grant_id": REVIEW_ROUND_GRANT_ID,
        # Checked against the schema here; the election replaces it below.
        "grant_event_id": 1,
        "repo": repo,
        "pr": pr,
        "round": round,
        "scope": scope,
    }
    validate_payload(GRANT_CITE_KIND, payload)
    validate_item_id(GRANT_CITE_KIND, item_id)
    _refuse_credentials(payload, item_id, stamps)
    with lg.transaction():
        _require_pr_item(lg, item_id, repo, pr)
        grant = standing(lg, REVIEW_ROUND_GRANT_ID)
        payload["grant_event_id"] = grant.deciding_event_id
        event = lg._append_receipt_event(
            _RECEIPT_WRITER_CAPABILITY,
            kind=GRANT_CITE_KIND,
            payload=payload,
            item_id=item_id,
            idempotency_key=cite_key(item_id, repo, pr, round, grant.deciding_event_id),
            **stamps,
        )
    return event


@dataclass(frozen=True)
class MergeRecord:
    event: Event
    state: str
    created: bool


def _write_merge(
    lg: Ledger,
    item_id: str,
    fields: Mapping[str, Any],
    kind: str,
    *,
    actor: str | None,
    harness: str | None,
    model: str | None,
    session: str | None,
) -> MergeRecord:
    payload = dict(fields)
    validate_payload(kind, payload)
    validate_item_id(kind, item_id)
    stamps = {"actor": actor, "harness": harness, "model": model, "session": session}
    _refuse_credentials(payload, item_id, stamps)
    with lg.transaction():
        repo, pr = payload["pr"].removeprefix("https://github.com/").rsplit("/pull/", 1)
        _require_pr_item(lg, item_id, repo, int(pr))
        intent = None
        done = None
        for event in lg.events(item_id=item_id):
            if event.payload.get("key") != payload["key"]:
                continue
            if event.kind == MERGE_INTENT_KIND:
                intent = event
            elif event.kind == MERGE_DONE_KIND:
                done = event
        if done is not None:
            return MergeRecord(done, "finished", False)
        if kind == MERGE_INTENT_KIND and intent is not None:
            return MergeRecord(intent, "started", False)
        if kind == MERGE_DONE_KIND and intent is None:
            raise _refuse("merge_intent_missing", "merge done requires an intent on this item/key")
        event = lg._append_receipt_event(
            _RECEIPT_WRITER_CAPABILITY,
            kind=kind,
            payload=payload,
            item_id=item_id,
            idempotency_key=f"{kind}:{item_id}:{payload['key']}",
            **stamps,
        )
        return MergeRecord(event, "finished" if kind == MERGE_DONE_KIND else "started", True)


def write_merge_intent(
    lg: Ledger,
    item_id: str,
    fields: Mapping[str, Any],
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> MergeRecord:
    """Start a merge, or return its existing started/finished record."""
    return _write_merge(lg, item_id, fields, MERGE_INTENT_KIND,
                        actor=actor, harness=harness, model=model, session=session)


def write_merge_done(
    lg: Ledger,
    item_id: str,
    fields: Mapping[str, Any],
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> MergeRecord:
    """Finish a started merge, or return its existing finished record."""
    return _write_merge(lg, item_id, fields, MERGE_DONE_KIND,
                        actor=actor, harness=harness, model=model, session=session)


__all__ = [
    "MergeRecord",
    "RECEIPT_DOC_MIME",
    "cite_key",
    "pr_item",
    "pr_url",
    "receipt_key",
    "write_grant_cite",
    "write_merge_done",
    "write_merge_intent",
    "write_receipt",
]
