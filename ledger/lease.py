"""A claim's lease: how long a holder keeps an item without showing life.

A claim (``item.state`` with status ``claimed``) carries ``expires_at``, a
UTC stamp in the ledger's own ``recorded_at`` form (millisecond precision, so
the stamps order as plain text). A loop ``item.progress`` event carries a
fresh ``expires_at`` and extends the lease. Once the stamp passes, the
``v_leases`` view marks the claim expired, ``v_ready`` lists the item as open,
and another actor's claim takes it over, naming the holder it replaced.

A claim recorded before leases existed has no ``expires_at`` and never
expires, exactly as before.

The TTL comes from ``claim_ttl_seconds`` in the ledger's ``config.json`` (the
file beside the database that also holds ``search_roots``), else
``DEFAULT_TTL_SECONDS``.

A malformed or out-of-range ``config.json`` makes ``claim`` refuse (exit 2)
unless ``--ttl`` is passed; the TTL is capped at ``MAX_TTL_SECONDS``.

Clock seam: ``now()`` is the one place this module reads the time. A test
replaces ``ledger.lease.now`` to move the clock; nothing here sleeps. The
SQL views read the database's own clock, so a test that needs a view to see
an expired claim records the claim at an earlier ``now()``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Four hours: longer than one build or review round, short enough that a dead
# seat does not hold a row through the next working day. A live seat renews it
# by recording progress.
DEFAULT_TTL_SECONDS = 4 * 60 * 60
# DESIGN: a lease is at most 30 days. Longer is a typo, and the stamp must stay
# inside the calendar the views and parse_stamp can read.
MAX_TTL_SECONDS = 30 * 24 * 60 * 60
CONFIG_KEY = "claim_ttl_seconds"


class LeaseConfigError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def expires_at(ttl_seconds: int, *, at: datetime | None = None) -> str:
    return stamp((at or now()) + timedelta(seconds=ttl_seconds))


def check_ttl(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TTL_SECONDS:
        raise LeaseConfigError(f"{name} must be an integer from 1 to {MAX_TTL_SECONDS} (seconds)")
    return value


def ttl_from_config(config: Mapping[str, Any]) -> int:
    """The configured TTL in seconds; the default when the key is absent."""
    if CONFIG_KEY not in config:
        return DEFAULT_TTL_SECONDS
    return check_ttl(config[CONFIG_KEY], CONFIG_KEY)


def ttl_for_db(db: Path | str) -> int:
    from ledger import search  # config.json is read in one place

    try:
        return ttl_from_config(search.load_config(db))
    except search.SearchError as exc:
        raise LeaseConfigError(str(exc)) from exc


_UNSET_CLAIM = object()


def _different_holder(row, requester, requester_claim) -> bool:
    holder = row["owner"] or row["claim"]
    if requester_claim is _UNSET_CLAIM:
        return bool(holder and requester and holder != requester)
    return (holder, row["claim"]) != (requester, requester_claim)


def takeover_holder(row, lease_row, requester, moment, *, requester_claim=_UNSET_CLAIM) -> str | None:
    """The expired holder a claim by ``requester`` replaces, else None."""
    if row is None or row["status"] != "claimed" or lease_row is None:
        return None
    holder = row["owner"] or row["claim"]
    if not _different_holder(row, requester, requester_claim):
        return None
    return holder if is_expired(lease_row["expires_at"], at=moment) else None


class ClaimRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def guard_claim(row, lease_row, requester, moment, recorded_prior, *, requester_claim=_UNSET_CLAIM) -> None:
    """The claim precondition, run inside the write transaction by every claim writer.

    Another holder's live claim is refused; an expired one may be taken over
    only when ``recorded_prior`` names exactly that holder.
    Supplying ``requester_claim`` compares both names; omitting it retains
    the single-holder contract used by the loop writer.
    """
    expected = takeover_holder(
        row, lease_row, requester, moment, requester_claim=requester_claim
    )
    if row is not None and row["status"] == "claimed" and expected is None:
        holder = row["owner"] or row["claim"]
        if _different_holder(row, requester, requester_claim):
            raise ClaimRefused("claimed", f"item already claimed by {holder}")
    if expected != recorded_prior:
        raise ClaimRefused(
            "claim_changed", "the claim changed while this claim was being recorded; retry"
        )


def current_lease(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    """The item's lease row while its latest state is a claim, else None."""
    cursor = conn.execute("SELECT * FROM v_leases WHERE item_id = ?", (item_id,))
    cursor.row_factory = sqlite3.Row
    row = cursor.fetchone()
    return None if row is None else dict(row)


def is_expired(expires: str | None, *, at: datetime | None = None) -> bool:
    """A lease without a stamp never expires."""
    if expires is None:
        return False
    return expires <= stamp(at or now())


def expired_leases(conn: sqlite3.Connection, *, at: datetime | None = None) -> list[dict[str, Any]]:
    """Every expired claim with its holder and age, plus every claim whose stamp is unreadable.

    Live and unstamped claims never appear. An unreadable stamp is listed
    with ``unreadable_stamp`` wherever its text sorts, a future-dated one
    included, because the stamp is parsed before expiry is decided.
    """
    moment = at or now()
    cursor = conn.execute("SELECT * FROM v_leases ORDER BY item_id")
    cursor.row_factory = sqlite3.Row
    out = []
    for raw in cursor.fetchall():
        row = dict(raw)
        if row["expires_at"] is None:
            continue
        try:
            ends = parse_stamp(row["expires_at"])
            began = parse_stamp(row["claimed_at"])
        except (ValueError, TypeError):
            # A stamp written before the schema checked the calendar: report it
            # as unreadable instead of hiding it or every other expired claim.
            row["unreadable_stamp"] = True
            row["expired_for_seconds"] = row["age_seconds"] = None
            out.append(row)
            continue
        if is_expired(row["expires_at"], at=moment):
            row["expired_for_seconds"] = _seconds(moment - ends)
            row["age_seconds"] = _seconds(moment - began)
            out.append(row)
    return out


def _seconds(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds()))


# DESIGN: a claim with no lease stamp (recorded before leases) never expires
# and never reads as open. Once its claim event is older than this it is
# stale: doctor lists it with a release command it prints and never runs, and
# query flags it. Nothing releases it automatically.
STALE_CLAIM_SECONDS = 7 * 24 * 60 * 60


def read_status(
    row: Mapping[str, Any],
    lease_row: Mapping[str, Any] | None,
    *,
    at: datetime | None = None,
) -> str:
    """The status a read verb shows for the item ``row``.

    Read side only: every guard and write path keeps reading the recorded
    status, and nothing here writes. ``lease_row`` is the item's ``v_leases``
    row (``current_lease``). The item reads ``open`` exactly when its recorded
    status is ``claimed`` and ``is_expired`` holds on that row's
    ``expires_at`` stamp, the test ``takeover_holder`` makes before a
    takeover. Otherwise it reads its recorded status. A claim with no stamp,
    or a stamp that does not parse, keeps ``claimed``: an unreadable stamp
    never makes a claim look free.
    """
    status = row["status"]
    if status != "claimed" or lease_row is None or lease_row["item_id"] != row["item_id"]:
        return status
    expires = lease_row["expires_at"]
    if expires is None:
        return status
    try:
        parse_stamp(expires)
    except (ValueError, TypeError):
        return status
    return "open" if is_expired(expires, at=at) else status


def stale_age(lease_row: Mapping[str, Any] | None, *, at: datetime | None = None) -> int | None:
    """Seconds since a stamp-less claim was recorded, once that is past
    ``STALE_CLAIM_SECONDS``; else None (a stamped claim is never stale)."""
    if lease_row is None or lease_row["expires_at"] is not None:
        return None
    try:
        began = parse_stamp(lease_row["claimed_at"])
    except (ValueError, TypeError):
        return None
    age = _seconds((at or now()) - began)
    return age if age > STALE_CLAIM_SECONDS else None


def stale_claims(conn: sqlite3.Connection, *, at: datetime | None = None) -> list[dict[str, Any]]:
    """Every stamp-less claim older than ``STALE_CLAIM_SECONDS``, with its holder and age.

    Disjoint from ``expired_leases``, which lists only stamped claims.
    """
    moment = at or now()
    cursor = conn.execute("SELECT * FROM v_leases WHERE expires_at IS NULL ORDER BY item_id")
    cursor.row_factory = sqlite3.Row
    out = []
    for raw in cursor.fetchall():
        row = dict(raw)
        age = stale_age(row, at=moment)
        if age is not None:
            row["age_seconds"] = age
            out.append(row)
    return out
