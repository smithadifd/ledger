"""Append several validated events for one item in a single transaction.

Stdin is a JSON array of objects. Each object has ``kind`` and ``payload``
(a JSON object) and may add ``item_id`` (which must equal the item argument),
``idempotency_key``, ``payload_version``, ``channel`` and ``provenance_class``:
the names ``append`` takes from its flags. Any other field is refused, and so
is a string field that cannot be encoded as UTF-8 (a JSON escape for a lone
surrogate), before the database is opened.

All events land or none do. ``--expect-revision`` is checked once, inside the
transaction, before the first event that is not already stored is written.
Every event keeps its own idempotency key, with ``append``'s retry rule: a
stored key returns the stored event and writes nothing. Replaying a whole
stored batch therefore writes nothing and needs no revision check. A partial
replay (some keys stored, some new) writes only the new events, in order, and
the revision check, when given, applies to the first new event.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from ledger.schema import ValidationError, schema_for, strip_directory_ref_slash
from ledger.verbs import (
    Context,
    Result,
    UsageError,
    Verb,
    actor_kwargs,
    add_expect_revision_argument,
    event_dict,
    expect_revision_value,
    new_key,
    open_db,
    require_item,
)

REQUIRED_FIELDS = ("kind", "payload")
OPTIONAL_FIELDS = (
    "item_id",
    "idempotency_key",
    "payload_version",
    "channel",
    "provenance_class",
)
ALLOWED_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS


def add_arguments(parser) -> None:
    parser.add_argument("item")
    add_expect_revision_argument(parser)


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _require_utf8(where: str, name: str, value: str) -> None:
    # A JSON escape for a lone surrogate decodes to a character that no UTF-8
    # text can hold; refuse it here rather than fail while writing.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UsageError(f"{where} {name} must be UTF-8 text: {exc.reason}") from exc


def _read_batch(item: str) -> list[dict[str, Any]]:
    try:
        raw = sys.stdin.read()
    except UnicodeDecodeError as exc:
        raise UsageError(f"stdin is not UTF-8 text: {exc}") from exc
    try:
        batch = json.loads(raw, parse_constant=_refuse_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise UsageError(f"stdin is not JSON: {exc}") from exc
    if type(batch) is not list:
        raise UsageError("stdin must be a JSON array of event objects")
    if not batch:
        raise UsageError("stdin holds no events")
    specs: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, element in enumerate(batch):
        where = f"event {index}"
        if type(element) is not dict:
            raise UsageError(f"{where} must be a JSON object")
        unknown = sorted(set(element) - set(ALLOWED_FIELDS))
        if unknown:
            raise UsageError(f"{where} has unknown field: {', '.join(unknown)}")
        for name in REQUIRED_FIELDS:
            if name not in element:
                raise UsageError(f"{where} is missing field: {name}")
        if type(element["kind"]) is not str or not element["kind"]:
            raise UsageError(f"{where} kind must be a non-empty string")
        _require_utf8(where, "kind", element["kind"])
        if type(element["payload"]) is not dict:
            raise UsageError(f"{where} payload must be a JSON object")
        for name in ("item_id", "idempotency_key", "channel", "provenance_class"):
            value = element.get(name)
            if name in element and (type(value) is not str or not value):
                raise UsageError(f"{where} {name} must be a non-empty string")
            if name in element:
                _require_utf8(where, name, value)
        version = element.get("payload_version", 1)
        if type(version) is not int:
            raise UsageError(f"{where} payload_version must be an integer")
        if "item_id" in element and element["item_id"] != item:
            raise UsageError(
                f"{where} is for item {element['item_id']!r}, not {item!r}"
            )
        key = element.get("idempotency_key")
        if key is not None:
            if key in seen_keys:
                raise UsageError(f"{where} repeats idempotency_key {key!r}")
            seen_keys.add(key)
        specs.append(
            {
                "kind": element["kind"],
                "payload": element["payload"],
                "payload_version": version,
                "channel": element.get("channel"),
                "provenance_class": element.get("provenance_class"),
                "idempotency_key": key,
            }
        )
    return specs


def run(args, ctx: Context) -> Result:
    expect = expect_revision_value(args)
    specs = _read_batch(args.item)
    lg = open_db(ctx, write=True)
    try:
        events = []
        pending_expect = expect
        with lg.transaction():
            for spec in specs:
                key = spec["idempotency_key"] or new_key()
                if spec["kind"] != "item.created":
                    try:
                        mode = schema_for(spec["kind"], spec["payload_version"]).get(
                            "item_id"
                        )
                    except ValidationError:
                        mode = None
                    if mode == "required":
                        require_item(lg, args.item)
                stored = lg.event_by_key(key)
                # The revision is checked once: on the first event that will
                # be written. A stored key is a retry and skips it, as it does
                # for `append`.
                check = None if stored is not None else pending_expect
                if stored is None:
                    pending_expect = None
                events.append(
                    lg.append(
                        kind=spec["kind"],
                        payload=strip_directory_ref_slash(
                            spec["kind"], spec["payload"], spec["payload_version"]
                        ),
                        item_id=args.item,
                        idempotency_key=key,
                        payload_version=spec["payload_version"],
                        channel=spec["channel"],
                        provenance_class=spec["provenance_class"],
                        expect_revision=check,
                        **actor_kwargs(ctx),
                    )
                )
    finally:
        lg.close()
    lines = [
        f"appended\t{event.kind}\tevent={event.id}\titem={event.item_id or '-'}"
        for event in events
    ]
    return Result(
        data={"item_id": args.item, "events": [event_dict(event) for event in events]},
        text="\n".join(lines),
    )


VERB = Verb(
    name="append-many",
    help="append several events for one item in one transaction (JSON array on stdin)",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
