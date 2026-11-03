"""Pool guidance: a dated, per-person note about one pool.

Guidance says a pool should be burned (spent down before its reset) or
protected (kept back), why, from when and until when. It lives in the local
ledger as an item of type ``guidance`` so that every seat reads one home and
nothing about it ships in a tracked file: it is dated and belongs to one
person.

One item stands for one pool and direction (``guidance-<pool>-<direction>``).
Setting it again appends a new ``guidance.set`` event and the latest event
wins, so extending or rewording guidance never rewrites history. Each set is
its own event even when its text matches an earlier one, so going back to
earlier guidance puts that guidance back in force. Closing the item (an
``item.state`` of ``closed``) withdraws it early; setting it again afterwards
reopens the item so the new guidance is in force.

Guidance is advice that is shown beside the meters. Nothing here changes which
models may do which work, and it never changes a review-family requirement:
it is honoured inside the standard, never over it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .schema import GUIDANCE_DIRECTIONS, GUIDANCE_ITEM_TYPE, GUIDANCE_KIND
from .store import IdempotencyConflict, Ledger, _same_semantic, open_ledger_readonly

CHANNEL = "guidance"


class GuidanceError(ValueError):
    """Guidance that cannot be recorded or read."""


@dataclass(frozen=True)
class Guidance:
    item_id: str
    pool: str
    direction: str
    reason: str
    starts: date
    expires: date


def item_id_for(pool: str, direction: str) -> str:
    return f"guidance-{pool}-{direction}"


def _parse_date(value: Any, name: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise GuidanceError(f"{name} must be an ISO date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise GuidanceError(f"{name} must be an ISO date (YYYY-MM-DD): {value!r}") from exc
    if parsed.isoformat() != value:
        raise GuidanceError(f"{name} must be an ISO date (YYYY-MM-DD): {value!r}")
    return parsed


def resolve_db(explicit: str | None, env: Mapping[str, str]) -> Path | None:
    """The ledger path guidance is read from: ``explicit``, else ``LEDGER_DB``.

    The path must be absolute so that a seat started in any folder reads the
    same database; a relative path would resolve against whichever folder the
    seat happened to start in. ``None`` when neither is set.
    """
    value = explicit or env.get("LEDGER_DB") or ""
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise GuidanceError(f"ledger path must be absolute, got {value!r}")
    return path


def record_guidance(
    lg: Ledger,
    *,
    pool: str,
    direction: str,
    reason: str,
    starts: date | str,
    expires: date | str,
    operation_id: str | None = None,
    **labels: Any,
) -> Guidance:
    """Write (or re-set) one guidance item; ``labels`` are the usual actor fields.

    ``operation_id`` names this one set. A retry of the same set passes the
    same id and is recorded once, returning what the first call recorded and
    changing nothing, even when the item has been withdrawn since; a later set
    gets a new id, so it is a new event even when its text matches earlier
    guidance. Without one, each call is its own set.

    The item's status is read, a withdrawn item reopened and the guidance
    appended in one write transaction, so a withdraw from another writer
    lands either before this set (and is reopened by it) or after it.
    """
    if direction not in GUIDANCE_DIRECTIONS:
        raise GuidanceError(f"direction must be one of {list(GUIDANCE_DIRECTIONS)}, got {direction!r}")
    start_day = _parse_date(starts, "starts")
    expire_day = _parse_date(expires, "expires")
    if expire_day < start_day:
        raise GuidanceError("expires must not be before starts")
    item_id = item_id_for(pool, direction)
    payload = {
        "pool": pool,
        "direction": direction,
        "reason": reason,
        "starts": start_day.isoformat(),
        "expires": expire_day.isoformat(),
    }
    operation = operation_id or uuid.uuid4().hex
    # The status read must sit inside the write transaction: read before it,
    # a withdraw committed in between would leave this set stored but closed.
    set_key = f"guidance:{item_id}:{operation}:set"
    with lg.transaction():
        # A set that already ran is answered from its record before anything
        # else is written: a retry after a withdraw must not reopen the item.
        done = lg.event_by_key(set_key)
        if done is not None:
            # The ledger's own comparison, on kind, item and payload only: a
            # retry from a later session or actor is still the same set, but
            # changed guidance under the same id is a conflict.
            if not _same_semantic(
                done,
                kind=GUIDANCE_KIND,
                payload_version=1,
                payload=payload,
                item_id=item_id,
                actor=None,
                harness=None,
                model=None,
                session=None,
                channel=None,
                provenance_class=None,
                ignore_attribution=True,
            ):
                raise IdempotencyConflict(
                    f"idempotency_key {set_key!r} already recorded as a different event"
                )
            return _from_payload(item_id, done.payload)
        existing = lg.item(item_id)
        if existing is None:
            lg.append(
                kind="item.created",
                payload={"type": GUIDANCE_ITEM_TYPE, "title": f"{direction} {pool}"},
                item_id=item_id,
                idempotency_key=f"guidance:{item_id}:created",
                channel=CHANNEL,
                **labels,
            )
        elif existing["status"] == "closed":
            # A withdrawn item must be reopened, or the new guidance is stored
            # but never listed or printed.
            lg.append(
                kind="item.state",
                payload={"status": "open"},
                item_id=item_id,
                idempotency_key=f"guidance:{item_id}:{operation}:reopen",
                channel=CHANNEL,
                **labels,
            )
        # Keyed by the operation, never by the text: a key built from the
        # text alone would fold a later identical set into the earlier event.
        lg.append(
            kind=GUIDANCE_KIND,
            payload=payload,
            item_id=item_id,
            idempotency_key=set_key,
            channel=CHANNEL,
            **labels,
        )
    return Guidance(item_id, pool, direction, reason, start_day, expire_day)


def _from_payload(item_id: str, payload: Mapping[str, Any]) -> Guidance:
    return Guidance(
        item_id=item_id,
        pool=payload["pool"],
        direction=payload["direction"],
        reason=payload["reason"],
        starts=date.fromisoformat(payload["starts"]),
        expires=date.fromisoformat(payload["expires"]),
    )


def _latest(lg: Ledger, item_id: str) -> Guidance | None:
    events = lg.events(item_id=item_id, kind=GUIDANCE_KIND)
    if not events:
        return None
    return _from_payload(item_id, events[-1].payload)


def in_force(lg: Ledger, today: date) -> list[Guidance]:
    """Guidance whose window covers ``today``, oldest item first.

    The window is inclusive at both ends: guidance expires on the day after
    its ``expires`` date. A withdrawn (closed) item is never in force.
    """
    found: list[Guidance] = []
    for item in lg.items():
        if item["type"] != GUIDANCE_ITEM_TYPE or item["status"] == "closed":
            continue
        guidance = _latest(lg, item["item_id"])
        if guidance is None:
            continue
        if guidance.expires < today:
            continue
        if guidance.starts > today:
            continue
        found.append(guidance)
    return found


def read_in_force(db: Path | str, today: date) -> list[Guidance]:
    """Open ``db`` read-only and return the guidance in force on ``today``."""
    with open_ledger_readonly(db) as lg:
        return in_force(lg, today)
