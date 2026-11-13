"""Set, show or diff an item's spec: its content as immutable versions.

A spec is one ``item.spec`` event per version. ``spec set`` appends version
N+1 only when the content hash or the source differs from the current
spec's; the same content with the same source again writes nothing and
reports ``unchanged``. The content hash covers exactly the content fields
(``SPEC_CONTENT_FIELDS``: title, body_md, acceptance, size, merge_class), so
setting the same content from a newer commit (another ``--source-rev``) is a
no-op. The version is computed inside
the write transaction. The latest ``item.spec`` event is the current spec;
``v_items.spec_version`` names its version.

``set_spec`` is the only writer of ``item.spec``: the store reserves the
kind, so ``ledger append`` and ``append-many`` refuse it and no version can
skip the N+1 and content-hash rules. A merge class is one of MC-0 to MC-4.

``spec set --expect-revision ID`` is the compare-and-set ``append``,
``state``, ``note`` and ``append-many`` take, parsed by the same code: inside
the write transaction, after the not-found check and before the unchanged
compare, the item's newest event id must still be ID. Otherwise
``StaleRevision`` (exit 5, ``stale_revision``) is raised and nothing is
written, even when the content and source are unchanged. ``none`` never
matches, since every item has its creation event.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from ledger.schema import (
    SPEC_CONTENT_FIELDS,
    SPEC_KIND,
    SPEC_MERGE_CLASSES,
    SPEC_SIZES,
    ValidationError,
)
from ledger.store import Event, Ledger, _check_expected_revision, canonical_dumps
from ledger.verbs import (
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    actor_kwargs,
    add_expect_revision_argument,
    event_dict,
    expect_revision_value,
    open_db,
)

ACTIONS = ("set", "show", "diff")
PROVENANCE_FIELDS = ("source", "source_rev")
_SET_ONLY = (
    "title",
    "body",
    "body_file",
    "acceptance",
    "size",
    "merge_class",
    "source",
    "source_rev",
    "expect_revision",
)
_VERSION = re.compile(r"v?([1-9][0-9]{0,8})")
# The store appends item.spec only for a caller holding this (set_spec).
_SPEC_WRITER_CAPABILITY = object()


def content_hash(content: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON (sorted keys, no whitespace) of the
    content fields only; an absent field hashes as null."""
    body = {name: content.get(name) for name in SPEC_CONTENT_FIELDS}
    return hashlib.sha256(canonical_dumps(body).encode("utf-8")).hexdigest()


def spec_events(lg: Ledger, item_id: str) -> list[Event]:
    """The item's ``item.spec`` events, oldest first."""
    return lg.events(item_id=item_id, kind=SPEC_KIND)


def current_spec(lg: Ledger, item_id: str) -> Event | None:
    """The item's latest ``item.spec`` event, or ``None``."""
    specs = spec_events(lg, item_id)
    return specs[-1] if specs else None


def set_spec(
    lg: Ledger,
    item_id: str,
    content: Mapping[str, Any],
    *,
    source: str | None = None,
    source_rev: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
    expect_revision: int | str | None = None,
) -> tuple[Event, bool]:
    """Append the item's next spec version when its content or source changed.

    Returns ``(event, created)``: the new event and True, or the current
    spec's event and False when the content hash is unchanged and ``source``
    is absent or equal to the current spec's (nothing is written). A different
    ``source`` over the same content writes the next version with that
    content and the new source. A call with no ``source`` compares content
    only and writes no source, as before. The current spec, the compares and
    the version are all read inside the write transaction.

    ``expect_revision`` is a compare-and-set on the item: an event id, or
    ``"none"`` (which never matches, since every item has its creation
    event). It is checked inside the write transaction, after the not-found
    check and before the unchanged compare, so a stale value raises
    ``StaleRevision`` and writes nothing even when the content and source are
    unchanged. A value of another type is refused as ``append`` refuses it.
    ``None`` skips the check.
    """
    if expect_revision is not None and expect_revision != "none" and (
        type(expect_revision) is not int or expect_revision < 1
    ):
        raise ValidationError("bad-type:expect_revision")
    digest = content_hash(content)
    with lg.transaction():
        if lg.item(item_id) is None:
            raise CliError(
                f"item not found: {item_id}", code="not_found", exit_code=EXIT_NOT_FOUND
            )
        if expect_revision is not None:
            # Before the unchanged compare: a stale revision never reads as
            # an unchanged no-op.
            _check_expected_revision(lg._conn, item_id, expect_revision)
        current = current_spec(lg, item_id)
        if (
            current is not None
            and current.payload.get("content_hash") == digest
            and (source is None or current.payload.get("source") == source)
        ):
            return current, False
        version = 1 if current is None else int(current.payload["version"]) + 1
        payload: dict[str, Any] = {"version": version, "content_hash": digest}
        for name in SPEC_CONTENT_FIELDS:
            if content.get(name) is not None:
                payload[name] = content[name]
        if source is not None:
            payload["source"] = source
        if source_rev is not None:
            payload["source_rev"] = source_rev
        event = lg._append_spec_event(
            _SPEC_WRITER_CAPABILITY,
            kind=SPEC_KIND,
            payload=payload,
            item_id=item_id,
            idempotency_key=f"spec:{item_id}:v{version}",
            actor=actor,
            harness=harness,
            model=model,
            session=session,
        )
    return event, True


def add_arguments(parser) -> None:
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("item")
    parser.add_argument(
        "versions",
        nargs="*",
        metavar="VERSION",
        help="diff only: FROM [TO] (default: the previous version to the current)",
    )
    parser.add_argument("--title", help="set: the spec title")
    parser.add_argument("--body", help="set: the spec body (markdown)")
    parser.add_argument("--body-file", help="set: read the spec body from this file")
    parser.add_argument("--acceptance", help="set: the acceptance criteria")
    parser.add_argument("--size", choices=SPEC_SIZES, help="set: S, M or L")
    parser.add_argument(
        "--merge-class", choices=SPEC_MERGE_CLASSES, help="set: the merge class, MC-0 to MC-4"
    )
    parser.add_argument("--source", help="set: where the content came from (not hashed)")
    parser.add_argument("--source-rev", help="set: the commit it was read at (not hashed)")
    add_expect_revision_argument(parser)
    parser.add_argument("--version", help="show: this version instead of the current one")


def _parse_version(raw: str, name: str) -> int:
    match = _VERSION.fullmatch(raw)
    if match is None:
        raise UsageError(f"{name} must be a spec version such as 2 or v2, got {raw!r}")
    return int(match.group(1))


def _refuse_flags(args, names: tuple[str, ...], action: str) -> None:
    given = [name for name in names if getattr(args, name) is not None]
    if given:
        flags = ", ".join("--" + name.replace("_", "-") for name in given)
        raise UsageError(f"spec {action} does not take {flags}")


def _no_spec(item_id: str) -> CliError:
    return CliError(f"item {item_id} has no spec", code="no_spec", exit_code=EXIT_NOT_FOUND)


def _by_version(specs: list[Event], item_id: str, version: int) -> Event:
    matches = [event for event in specs if event.payload.get("version") == version]
    if not matches:
        current = specs[-1].payload.get("version")
        raise CliError(
            f"item {item_id} has no spec v{version} (current is v{current})",
            code="unknown_version",
            exit_code=EXIT_NOT_FOUND,
        )
    return matches[-1]


def _spec_data(item_id: str, event: Event) -> dict[str, Any]:
    payload = event.payload
    data: dict[str, Any] = {
        "item_id": item_id,
        "version": payload["version"],
        "event_id": event.id,
        "recorded_at": event.recorded_at,
        "content_hash": payload["content_hash"],
    }
    for name in SPEC_CONTENT_FIELDS + PROVENANCE_FIELDS:
        data[name] = payload.get(name)
    return data


def _show_text(data: Mapping[str, Any]) -> str:
    lines = [
        f"spec v{data['version']}\t{data['item_id']}\tevent={data['event_id']}",
        f"title: {data['title']}",
    ]
    for name in ("size", "merge_class") + PROVENANCE_FIELDS:
        if data.get(name) is not None:
            lines.append(f"{name}: {data[name]}")
    lines.append(f"content_hash: {data['content_hash']}")
    if data.get("acceptance") is not None:
        lines.extend(["", "## Acceptance", data["acceptance"]])
    lines.extend(["", "## Body", data["body_md"]])
    return "\n".join(lines)


def _lines(value: Any) -> list[str]:
    return [] if value is None else str(value).splitlines()


def diff_fields(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One entry per content field whose value differs, in field order."""
    changed = []
    for name in SPEC_CONTENT_FIELDS:
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        changed.append({
            "field": name,
            "from": before,
            "to": after,
            "diff": list(difflib.unified_diff(
                _lines(before), _lines(after),
                fromfile=f"v{old['version']}", tofile=f"v{new['version']}",
                lineterm="",
            )),
        })
    return changed


def _run_set(args, ctx: Context) -> Result:
    _refuse_flags(args, ("version",), "set")
    if args.versions:
        raise UsageError("spec set takes no versions")
    if not args.title:
        raise UsageError("spec set needs --title")
    if (args.body is None) == (args.body_file is None):
        raise UsageError("spec set needs exactly one of --body or --body-file")
    body = args.body
    if args.body_file is not None:
        try:
            body = Path(args.body_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError(f"--body-file: cannot read {args.body_file}: {exc}") from exc
    if not body:
        raise UsageError("spec set needs a non-empty body")
    expect = expect_revision_value(args)
    content = {
        "title": args.title,
        "body_md": body,
        "acceptance": args.acceptance,
        "size": args.size,
        "merge_class": args.merge_class,
    }
    lg = open_db(ctx, write=True)
    try:
        event, created = set_spec(
            lg,
            args.item,
            content,
            source=args.source,
            source_rev=args.source_rev,
            expect_revision=expect,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    version = event.payload["version"]
    return Result(
        data={
            "item_id": args.item,
            "version": version,
            "unchanged": not created,
            "event": event_dict(event),
        },
        text=f"spec\t{args.item}\tv{version}\t"
        + (f"event={event.id}" if created else "unchanged"),
    )


def _read_specs(ctx: Context, item_id: str) -> list[Event]:
    lg = open_db(ctx, write=False)
    try:
        if lg.item(item_id) is None:
            raise CliError(
                f"item not found: {item_id}", code="not_found", exit_code=EXIT_NOT_FOUND
            )
        specs = spec_events(lg, item_id)
    finally:
        lg.close()
    if not specs:
        raise _no_spec(item_id)
    return specs


def _run_show(args, ctx: Context) -> Result:
    _refuse_flags(args, _SET_ONLY, "show")
    if args.versions:
        raise UsageError("spec show takes --version, not positional versions")
    specs = _read_specs(ctx, args.item)
    event = specs[-1]
    if args.version is not None:
        event = _by_version(specs, args.item, _parse_version(args.version, "--version"))
    data = _spec_data(args.item, event)
    return Result(data=data, text=_show_text(data))


def _run_diff(args, ctx: Context) -> Result:
    _refuse_flags(args, _SET_ONLY + ("version",), "diff")
    if len(args.versions) > 2:
        raise UsageError("spec diff takes at most two versions: FROM [TO]")
    specs = _read_specs(ctx, args.item)
    current = specs[-1]
    if len(args.versions) == 2:
        old = _by_version(specs, args.item, _parse_version(args.versions[0], "FROM"))
        new = _by_version(specs, args.item, _parse_version(args.versions[1], "TO"))
    elif len(args.versions) == 1:
        old = _by_version(specs, args.item, _parse_version(args.versions[0], "FROM"))
        new = current
    else:
        new = current
        previous = int(current.payload["version"]) - 1
        if previous < 1:
            raise CliError(
                f"item {args.item} has only spec v{current.payload['version']}: "
                "no earlier version to diff against",
                code="unknown_version",
                exit_code=EXIT_NOT_FOUND,
            )
        old = _by_version(specs, args.item, previous)
    changed = diff_fields(old.payload, new.payload)
    head = f"spec diff {args.item} v{old.payload['version']} → v{new.payload['version']}"
    lines = [head]
    if not changed:
        lines.append("no content change")
    for entry in changed:
        lines.extend(["", f"## {entry['field']}", *entry["diff"]])
    return Result(
        data={
            "item_id": args.item,
            "from": old.payload["version"],
            "to": new.payload["version"],
            "changed": [entry["field"] for entry in changed],
            "fields": changed,
        },
        text="\n".join(lines),
    )


def run(args, ctx: Context) -> Result:
    if args.action == "set":
        return _run_set(args, ctx)
    if args.action == "show":
        return _run_show(args, ctx)
    return _run_diff(args, ctx)


VERB = Verb(
    name="spec",
    help="set, show or diff an item's spec (versioned ticket content)",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
