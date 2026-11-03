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


def takeover_holder(row, lease_row, requester, moment) -> str | None:
    """The expired holder a claim by ``requester`` replaces, else None."""
    if row is None or row["status"] != "claimed" or lease_row is None:
        return None
    holder = row["owner"] or row["claim"]
    if not (holder and requester and holder != requester):
        return None
    return holder if is_expired(lease_row["expires_at"], at=moment) else None


class ClaimRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def guard_claim(row, lease_row, requester, moment, recorded_prior) -> None:
    """The claim precondition, run inside the write transaction by every claim writer.

    Another holder's live claim is refused; an expired one may be taken over
    only when ``recorded_prior`` names exactly that holder.
    """
    expected = takeover_holder(row, lease_row, requester, moment)
    if row is not None and row["status"] == "claimed" and expected is None:
        holder = row["owner"] or row["claim"]
        if holder and requester and holder != requester:
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
