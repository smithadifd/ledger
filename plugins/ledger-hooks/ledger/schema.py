"""Closed payload schemas for ledger event kinds.

Adding a kind is a new entry in ``SCHEMAS``, not a code fork. Unknown kinds
and unknown keys are refused. Every kind also has one entry in
``DURABILITY``: durable events stay in item exports and cards for good;
ephemeral events (progress bookkeeping) decay out of them once the item is
closed. The events themselves are never removed from the store.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Mapping

GUIDANCE_ITEM_TYPE = "guidance"
GUIDANCE_KIND = "guidance.set"
GUIDANCE_DIRECTIONS = ("burn", "protect")
ITEM_TYPES = ("topic", "ticket", "initiative", "decision", "gate", GUIDANCE_ITEM_TYPE)
ITEM_STATUSES = ("open", "claimed", "blocked", "closed")
# duplicate-of lives on the duplicate and names the item kept; `ledger link`
# closes the duplicate in the same transaction. child-of lives on the child
# and names its one anchor (the store's append path keeps the graph a forest).
LINK_RELS = (
    "pr", "branch", "session", "item", "url", "blocks", "duplicate-of", "child-of",
)
LOOP_OPERATIONS = ("build", "decision", "review", "merge", "ping")
# A decision's standing is a position, never a ticket status: these values
# share nothing with ITEM_STATUSES, and only decision events carry them.
STANDINGS = ("lean", "settled", "superseded")
PHASES = ("orient", "plan", "build", "review", "close")
WHO_KINDS = ("person", "role")
# Only the person-typed value exists; a value is added when a writer uses it.
UTTERANCE_ORIGINS = ("person",)
TAG_TYPES = ("ticket", "initiative", "team", "project")
OUTSIDE_EVENTS = ("merge", "reply", "ratification", "review", "release", "other")
SPEC_KIND = "item.spec"
SPEC_SIZES = ("S", "M", "L")
# The trust ladder's merge classes: a spec names one of these or none.
SPEC_MERGE_CLASSES = ("MC-0", "MC-1", "MC-2", "MC-3", "MC-4")
# The content fields of an item.spec payload: exactly these are hashed into
# its content_hash (verbs/spec.py). version, source, source_rev and the hash
# itself are not content.
SPEC_CONTENT_FIELDS = ("title", "body_md", "acceptance", "size", "merge_class")
# A document attached to an item (verbs/doc.py): content-addressed, its
# bytes stored inline (at most DOC_INLINE_LIMIT of them) or by path under the
# database's document root (at most DOC_PATH_LIMIT).
DOC_KIND = "doc.attached"
DOC_ROLES = ("review-receipt", "brief", "packet", "retro", "other")
DOC_INLINE_LIMIT = 65536
DOC_PATH_LIMIT = 16777216
# A by-path document's stored path: relative to the document root, at most
# this many UTF-8 bytes in all, and per component (doc_path_problem).
DOC_PATH_MAX_BYTES = 1024
DOC_PATH_COMPONENT_MAX_BYTES = 255
# The reason a stored path that breaks its grammar is refused with.
DOC_PATH_REFUSAL = "bad-doc-path"
DOC_DEFAULT_MIME = "application/octet-stream"
# Set, only as true, on a document whose bytes or stored path hold a
# credential shape (ledger/credscan.py) and that
# ``doc add --allow-credential-shapes`` attached.
DOC_CREDENTIAL_OVERRIDE = "credential_override"
# Channels only `ledger capture` (verbs/capture.py) writes: the harness's
# record of a submitted prompt and of a question-picker answer. The store
# refuses every other writer on any channel that starts with the prefix.
CAPTURE_CHANNEL_PREFIX = "hook:"
PROMPT_CHANNEL = "hook:prompt"
PICKER_CHANNEL = "hook:picker"
CAPTURE_CHANNELS = (PROMPT_CHANNEL, PICKER_CHANNEL)
_SOURCE = {"type": "string", "min_len": 1}
# A reference to a file, PR or event: one line, short. Content is never
# copied into the ledger, only pointed at.
_REF = {"type": "string", "min_len": 1, "max_len": 300, "single_line": True}
_TEXT = {"type": "string", "min_len": 1}
_SHA256_HEX = {"type": "string", "pattern": r"[0-9a-f]{64}"}
# A document's displayed text: one line, no control character
# (control_character, the Cc class), checked wherever the payload is
# validated, so a restored document holds none either.
_DOC_TITLE = {"type": "string", "min_len": 1, "max_len": 300, "single_line": True,
              "no_control": True}
_DOC_MIME = {
    "type": "string",
    "max_len": 127,
    "pattern": r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*",
}
# What the harness displayed in one AskUserQuestion call and what came
# back (verbs/capture.py builds it; ledger.answers reads it). display,
# answers and annotation are the harness's own data, kept verbatim.
_PICKER = {
    "type": "object",
    "required": {
        "display": {"type": "array", "min_len": 1, "items": {"type": "json_object"}},
        "display_sha256": _SHA256_HEX,
        "question_count": {"type": "integer", "min": 1},
        "answers": {"type": "json_object"},
        "typed": {"type": "boolean"},
        "ambiguous": {"type": "boolean"},
        "tool_use_id": {"type": "string", "min_len": 1},
    },
    "optional": {
        "chosen_label": {"type": "string"},
        "chosen_position": {"type": "integer", "min": 1},
        "annotation": {"type": "json_object"},
        "asked_event_id": {"type": "integer", "min": 1},
    },
}
# One fixed form (UTC, milliseconds) so lease stamps order as text.
_LEASE_STAMP = {
    "type": "string",
    "pattern": r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
    "calendar_stamp": True,  # also a real date and time, not just the shape
}
# What a resume card points the next seat at, and nothing else: a path
# (absolute, or repo-relative with a directory or an extension, optionally
# :line or :start-end), a PR or issue (owner/repo#N or #N), a branch
# (branch:<name>), a commit SHA, or a ledger event (event:<N>).
# A path segment is never "." or "..": a ref names a file, not a way out.
_PATH_SEGMENT = r"(?!\.{1,2}(?:[/:]|\Z))[A-Za-z0-9_.@+~-]+"
# git's ref-name rules (git check-ref-format): no "..", "@{", "//", control
# characters, whitespace or any of ~^:?*[\ ; no leading "-" or "/", no
# trailing "/" or "."; no component that starts with "." or ends in ".lock";
# and not "@" alone.
_BRANCH_NAME = (
    r"(?![-/])(?!@\Z)(?!.*\.\.)(?!.*@\{)(?!.*//)(?!.*[/.]\Z)"
    r"(?!.*\.lock(?:/|\Z))(?!(?:.*/)?\.)"
    r"[^\x00-\x20\x7f~^:?*\[\\]+"
)
_REF_TARGET_PATTERN = "|".join((
    rf"/(?:{_PATH_SEGMENT}/)*{_PATH_SEGMENT}(?::\d+(?:-\d+)?)?",
    rf"(?:{_PATH_SEGMENT}/)+{_PATH_SEGMENT}(?::\d+(?:-\d+)?)?",
    r"[A-Za-z0-9_.@+~-]*\.[A-Za-z0-9]+(?::\d+(?:-\d+)?)?",
    r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]*",
    rf"branch:{_BRANCH_NAME}",
    r"[0-9a-f]{7,40}",
    r"event:[1-9][0-9]*",
))
_REF_TARGET = {**_REF, "pattern": f"(?:{_REF_TARGET_PATTERN})"}
# A directory path, absolute or with at least one "/", no :line suffix: the
# only ref whose one trailing slash the writing verbs strip.
_DIRECTORY_REF = re.compile(
    rf"/(?:{_PATH_SEGMENT}/)*{_PATH_SEGMENT}|(?:{_PATH_SEGMENT}/)+{_PATH_SEGMENT}"
)
_SKILL = {
    "type": "string",
    "min_len": 1,
    "max_len": 80,
    "pattern": r"[A-Za-z0-9][A-Za-z0-9._:/-]*",
}

# The entity registry (verbs/entity.py writes it, v_entities reads it): an
# automation, project, machine or model, keyed by its type and a stable id.
ENTITY_REGISTERED_KIND = "entity.registered"
ENTITY_STATE_KIND = "entity.state"
ENTITY_TYPES = ("automation", "project", "machine", "model")
# One token, unique within its type: a launchd label, a repository name, a
# host name, a model id.
_ENTITY_ID = {
    "type": "string",
    "min_len": 1,
    "max_len": 200,
    "pattern": r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*",
}
# A short state value that cannot hold prose: one token, no whitespace.
_ENTITY_TOKEN = {
    "type": "string",
    "min_len": 1,
    "max_len": 64,
    "pattern": r"[A-Za-z0-9][A-Za-z0-9._-]*",
}
# The state an entity carries, in the order a rendered table shows it. An
# entity.state event names any non-empty subset; any other key is refused.
# schedule is the one free-text field (the leak lint's --ledger-db check
# reads it, with the title); wrapper is a path or command with no whitespace.
_ENTITY_STATE = {
    "schedule": {"type": "string", "min_len": 1, "max_len": 120, "single_line": True},
    "wrapper": {"type": "string", "min_len": 1, "max_len": 300, "pattern": r"\S+"},
    "host": _ENTITY_TOKEN,
    "risk_class": _ENTITY_TOKEN,
    "status": _ENTITY_TOKEN,
}
ENTITY_STATE_FIELDS = tuple(_ENTITY_STATE)

DURABLE = "durable"
EPHEMERAL = "ephemeral"
SYSTEM_CLAIM_KIND = "ledger.system"


# item_id: "required" | "optional" | "absent" (refused when given)
SCHEMAS: dict[tuple[str, int], dict[str, Any]] = {
    ("item.created", 1): {
        "item_id": "required",
        "required": {
            "type": {"enum": ITEM_TYPES},
            "title": {"type": "string", "min_len": 1},
        },
        "optional": {
            "status": {"enum": ITEM_STATUSES},
            "source": _SOURCE,
            # Which wave-slot queue (the SoT's `## Queue <letter>`) the item
            # belongs to and the repo it works in; the unattended wave slot
            # reads both to build its write envelope.
            "queue": {"type": "string", "min_len": 1, "pattern": r"[A-Za-z]+"},
            "target_repo": {
                "type": "string",
                "min_len": 1,
                "pattern": r"(?!\.{1,2}\Z)[A-Za-z0-9._-]+",
            },
        },
    },
    ("item.state", 1): {
        "item_id": "required",
        "required": {
            "status": {"enum": ITEM_STATUSES},
        },
        "optional": {
            "owner": {"type": "string", "min_len": 1},
            "claim": {"type": "string", "min_len": 1},
            "blocked_by": {"type": "array", "items": {"type": "string", "min_len": 1}},
            "reason": {"type": "string", "min_len": 1},
            "source": _SOURCE,
            # A claim's lease (ledger.lease): the stamp it runs to, and the
            # expired holder a takeover replaced.
            "expires_at": _LEASE_STAMP,
            "prior_holder": {"type": "string", "min_len": 1},
            # The item.spec version a claim builds against (verbs/claim.py
            # and the loop's take stamp it; absent when the item has no spec).
            "spec_version": {"type": "integer", "min": 1},
        },
    },
    ("item.linked", 1): {
        "item_id": "required",
        "required": {
            "rel": {"enum": LINK_RELS},
            "target": {"type": "string", "min_len": 1},
        },
        "optional": {
            "source": _SOURCE,
            # A child-of move names the anchor it left.
            "moved_from": {"type": "string", "min_len": 1},
        },
    },
    ("item.note", 1): {
        "item_id": "required",
        "required": {
            "text": {"type": "string", "min_len": 1},
        },
        "optional": {
            "source": _SOURCE,
        },
    },
    # One immutable version of a ticket's content. ``ledger spec set``
    # appends version N+1 only when content_hash or source differs from the
    # current spec's; the latest item.spec event is the item's current spec.
    # content_hash is sha256 over the canonical JSON of SPEC_CONTENT_FIELDS
    # (an absent field hashes as null). source and source_rev say where the
    # content came from and are not hashed. The store reserves the kind for
    # set_spec (verbs/spec.py): generic append and append-many refuse it.
    (SPEC_KIND, 1): {
        "item_id": "required",
        "required": {
            "version": {"type": "integer", "min": 1},
            "title": {"type": "string", "min_len": 1},
            "body_md": {"type": "string", "min_len": 1},
            "content_hash": {"type": "string", "pattern": r"[0-9a-f]{64}"},
        },
        "optional": {
            "acceptance": {"type": "string", "min_len": 1},
            "size": {"enum": SPEC_SIZES},
            "merge_class": {"enum": SPEC_MERGE_CLASSES},
            "source": _SOURCE,
            "source_rev": {"type": "string", "min_len": 1, "max_len": 300,
                           "single_line": True},
        },
    },
    # A document attached to an item. sha256 and size are computed by the
    # ledger from the bytes it read: ``ledger doc add`` is the only live
    # writer (the store reserves the kind). Exactly one of content_b64 (the
    # bytes, base64, at most DOC_INLINE_LIMIT) and path (a stored path under
    # the database's document root, in doc_path_problem's grammar) is
    # present; both or neither is refused (exactly-one-of). size is at most
    # DOC_PATH_LIMIT for every document. Restore and replay recompute an
    # inline document's digest before inserting it; a by-path document holds
    # no bytes, so they check its schema and scan its stored path
    # (restore.py). credential_override is present only as true: doc add sets
    # it when --allow-credential-shapes attached bytes or a stored path
    # holding a credential shape, and any other value is refused.
    (DOC_KIND, 1): {
        "item_id": "required",
        "required": {
            "role": {"enum": DOC_ROLES},
            "sha256": _SHA256_HEX,
            "size": {"type": "integer", "min": 0, "max": DOC_PATH_LIMIT},
            "mime": _DOC_MIME,
        },
        "optional": {
            "content_b64": {"type": "string"},
            "path": {"type": "string", "doc_path": True},
            "title": _DOC_TITLE,
            DOC_CREDENTIAL_OVERRIDE: {"type": "boolean", "only_true": True},
        },
        "exactly_one_of": (("content_b64", "path"),),
    },
    ("decision.asked", 1): {
        "item_id": "required",
        "required": {
            "question": {"type": "string", "min_len": 1},
            "options": {
                "type": "array",
                "min_len": 1,
                "items": {
                    "type": "object",
                    "required": {"label": {"type": "string", "min_len": 1}},
                    "optional": {"description": {"type": "string"}},
                },
            },
        },
        "optional": {
            "recommendation": {"type": "string", "min_len": 1},
            "default": {"type": "string", "min_len": 1},
            "source": _SOURCE,
            "question_id": {"type": "string", "min_len": 1},
            "ask": {"type": "string", "min_len": 1},
            "effort": {"type": "string", "min_len": 1},
            "added": {"type": "string", "min_len": 1},
            "ask_kind": {"type": "string", "min_len": 1},
            "unblocks": {"type": "string", "min_len": 1},
            # The source entry's heading title as written, before cleaning.
            "title_raw": {"type": "string", "min_len": 1},
            # The source entry's raw markdown: every line under its heading.
            "body": {"type": "string", "min_len": 1},
            # What the answer covers. The picker shows it in the question
            # (ledger ask-display), and an election takes it from the ask.
            "scope": {"type": "string", "min_len": 1},
        },
    },
    ("decision.elected", 1): {
        "item_id": "required",
        "required": {
            "question": {"type": "string", "min_len": 1},
            "chosen": {"type": "string", "min_len": 1},
            "chosen_description": {"type": "string"},
            "rejected": {"type": "array", "items": {"type": "string"}},
            "scope": {"type": "string", "min_len": 1},
            "quote_ref": {"type": "integer", "min": 1},
        },
        "optional": {
            "quotes": {
                "type": "array",
                "items": {"type": "string", "min_len": 1},
            },
            "source": _SOURCE,
            "question_id": {"type": "string", "min_len": 1},
            "asked_event_id": {"type": "integer", "min": 1},
            # The picker answer an election records (ledger pick): the
            # fingerprint of the display the owner saw and the 1-based
            # position of the option chosen.
            "display_sha256": _SHA256_HEX,
            "chosen_position": {"type": "integer", "min": 1},
        },
    },
    # Withdraws (held) or restores (released) one ask without an election and
    # without touching item state; the latest marker on an ask wins. Without
    # asked_event_id it is an item-level marker: it hides nothing and only
    # records that the item was held (or released), for an item with no ask.
    ("decision.held", 1): {
        "item_id": "required",
        "required": {
            "status": {"enum": ("held", "released")},
        },
        "optional": {
            "asked_event_id": {"type": "integer", "min": 1},
            "source": _SOURCE,
        },
    },
    ("utterance.captured", 1): {
        "item_id": "optional",
        "required": {
            "text": {"type": "string", "min_len": 1},
        },
        "optional": {
            "prompt_id": {"type": "string", "min_len": 1},
            "session_id": {"type": "string", "min_len": 1},
            # Who typed it. Absent on events written before the field existed.
            "origin": {"enum": UTTERANCE_ORIGINS},
            # A prompt over the size cap is recorded as a pointer, never the
            # body: ``text`` is then a short fixed line, and these two say how
            # large the omitted body was and which bytes it was.
            "omitted_bytes": {"type": "integer", "min": 1},
            "omitted_sha256": {"type": "string", "pattern": r"[0-9a-f]{64}"},
            "source": _SOURCE,
            # Only on channel hook:picker, which only ledger capture writes.
            "picker": _PICKER,
        },
        "together": (("omitted_bytes", "omitted_sha256"),),
    },
    ("session.opened", 1): {
        "item_id": "optional",
        "required": {},
        "optional": {
            "purpose": {"type": "string", "min_len": 1},
            "source": _SOURCE,
        },
    },
    ("session.closed", 1): {
        "item_id": "optional",
        "required": {},
        "optional": {
            "outcome": {"type": "string", "min_len": 1},
            "source": _SOURCE,
        },
    },
    ("loop.managed", 1): {
        "item_id": "required",
        "required": {},
        "optional": {"source": _SOURCE},
    },
    ("loop.intent", 1): {
        "item_id": "required",
        "required": {
            "operation": {"enum": LOOP_OPERATIONS},
            "operation_id": {"type": "string", "min_len": 1},
        },
        "optional": {
            "target": {"type": "string", "min_len": 1},
            "head": {"type": "string", "min_len": 1},
            "asked_event_id": {"type": "integer", "min": 1},
            "source": _SOURCE,
        },
    },
    ("loop.done", 1): {
        "item_id": "required",
        "required": {
            "operation": {"enum": LOOP_OPERATIONS},
            "operation_id": {"type": "string", "min_len": 1},
            "intent_event_id": {"type": "integer", "min": 1},
        },
        "optional": {
            "target": {"type": "string", "min_len": 1},
            "head": {"type": "string", "min_len": 1},
            "verdict": {"enum": ("pass", "changes")},
            "outcome": {"type": "string", "min_len": 1},
            "asked_event_id": {"type": "integer", "min": 1},
            "source": _SOURCE,
        },
    },
    ("loop.closed", 1): {
        "item_id": "required",
        "required": {
            "merge_done_event_id": {"type": "integer", "min": 1},
        },
        "optional": {
            "outcome": {"type": "string", "min_len": 1},
            "source": _SOURCE,
        },
    },
    # One decision with its provenance. Settling or superseding it later is a
    # decision.standing event that names this one; nothing is rewritten.
    # ``who`` is whoever holds the position: a person, or a role as someone
    # relayed it (``relayed_by``), never recorded as the relayer's own words.
    ("decision.recorded", 1): {
        "item_id": "required",
        "required": {
            "who": _TEXT,
            "who_kind": {"enum": WHO_KINDS},
            "standing": {"enum": ("lean", "settled")},
            "effect": _TEXT,
            "tags": {
                "type": "array",
                "min_len": 1,
                "items": {
                    "type": "object",
                    "required": {"type": {"enum": TAG_TYPES}, "id": _REF},
                    "optional": {},
                },
            },
            "verbatim": _TEXT,
            "phase": {"enum": PHASES},
        },
        "optional": {
            "relayed_by": _TEXT,
            "source_event_id": {"type": "integer", "min": 1},
            "evidence": _REF,
            "source": _SOURCE,
        },
    },
    ("decision.standing", 1): {
        "item_id": "required",
        "required": {
            "decision_event_id": {"type": "integer", "min": 1},
            "standing": {"enum": STANDINGS},
        },
        "optional": {
            "source_event_id": {"type": "integer", "min": 1},
            "superseded_by": {"type": "integer", "min": 1},
            "note": _TEXT,
            "source": _SOURCE,
        },
    },
    # What came of a decision, filled in later.
    ("decision.result", 1): {
        "item_id": "required",
        "required": {
            "decision_event_id": {"type": "integer", "min": 1},
            "result": _TEXT,
        },
        "optional": {
            "evidence": _REF,
            "source": _SOURCE,
        },
    },
    # Something that happened outside the session (a merge, a reply, a
    # ratification), recorded so a decision can cite it when it settles.
    ("outside.recorded", 1): {
        "item_id": "optional",
        "required": {
            "what": {"enum": OUTSIDE_EVENTS},
            "ref": _REF,
        },
        "optional": {
            "who": _TEXT,
            "text": _TEXT,
            "source": _SOURCE,
        },
    },
    ("item.progress", 1): {
        "item_id": "required",
        "required": {"text": _TEXT},
        "optional": {
            "phase": {"enum": PHASES},
            "source": _SOURCE,
            "expires_at": _LEASE_STAMP,
        },
    },
    ("item.tried", 1): {
        "item_id": "required",
        "required": {"what": _TEXT},
        "optional": {
            "outcome": _TEXT,
            "phase": {"enum": PHASES},
            "evidence": _REF,
            "source": _SOURCE,
        },
    },
    ("review.recorded", 1): {
        "item_id": "required",
        "required": {
            "round": {"type": "integer", "min": 1},
            "verdict": {"enum": ("pass", "changes")},
        },
        "optional": {
            "findings": {"type": "array", "max_len": 20, "items": _TEXT},
            "reviewer": _TEXT,
            "evidence": _REF,
            "source": _SOURCE,
        },
    },
    # A stop or close stamp: where work stopped, the skills the next seat
    # invokes, and the files, PRs or events it should read, by path or id.
    ("item.stopped", 1): {
        "item_id": "required",
        "required": {
            "point": {"type": "string", "min_len": 1, "max_len": 600},
            "phase": {"enum": PHASES, "show_allowed": True},
        },
        "optional": {
            "skills": {"type": "array", "max_len": 12, "items": _SKILL},
            "refs": {"type": "array", "max_len": 20, "items": _REF_TARGET},
            "source": _SOURCE,
        },
    },
    # A dated note that a pool should be burned or protected, and why. The
    # latest event on a guidance item is the one in force; see guidance.py.
    (GUIDANCE_KIND, 1): {
        "item_id": "required",
        "required": {
            "pool": {"type": "string", "min_len": 1, "max_len": 80,
                     "pattern": r"[a-z][a-z0-9-]*"},
            "direction": {"enum": GUIDANCE_DIRECTIONS},
            "reason": {"type": "string", "min_len": 1, "max_len": 300, "single_line": True},
            "starts": {"type": "string", "pattern": r"\d{4}-\d{2}-\d{2}"},
            "expires": {"type": "string", "pattern": r"\d{4}-\d{2}-\d{2}"},
        },
        "optional": {"source": _SOURCE},
    },
    # One registered entity. Only register_entity (verbs/entity.py) writes
    # it: the store reserves the kind, and a second registration of the same
    # (type, id) is refused. The title is free text on one line.
    (ENTITY_REGISTERED_KIND, 1): {
        "item_id": "absent",
        "required": {
            "type": {"enum": ENTITY_TYPES},
            "id": _ENTITY_ID,
            "title": {"type": "string", "min_len": 1, "max_len": 200, "single_line": True},
        },
        "optional": {},
    },
    # A change to a registered entity's state: a non-empty map holding only
    # the fields whose value changed. Only set_entity_state (verbs/entity.py)
    # writes it; v_entities takes each field's value from its newest event.
    (ENTITY_STATE_KIND, 1): {
        "item_id": "absent",
        "required": {
            "type": {"enum": ENTITY_TYPES},
            "id": _ENTITY_ID,
            "state": {
                "type": "object",
                "min_keys": 1,
                "required": {},
                "optional": _ENTITY_STATE,
            },
        },
        "optional": {},
    },
    # The system (for example work or home) this database belongs to. Only
    # Ledger.claim_system writes it, once, so it rides the hash chain into
    # exports and restores. The write path, restore and verify each refuse
    # a second claim.
    (SYSTEM_CLAIM_KIND, 1): {
        "item_id": "optional",
        "required": {
            "system": {"type": "string", "max_len": 64, "pattern": r"[a-z][a-z0-9_-]*"},
        },
        "optional": {},
    },
}

# One place declares how long each kind lives in item exports and cards.
# A kind missing here counts as durable: keeping an event is always safe,
# dropping one is not.
DURABILITY: dict[str, str] = {
    "item.created": DURABLE,
    "item.state": DURABLE,
    "item.linked": DURABLE,
    "item.note": DURABLE,
    SPEC_KIND: DURABLE,
    DOC_KIND: DURABLE,
    ENTITY_REGISTERED_KIND: DURABLE,
    ENTITY_STATE_KIND: DURABLE,
    "item.tried": DURABLE,
    "item.stopped": DURABLE,
    "item.progress": EPHEMERAL,
    "decision.asked": DURABLE,
    "decision.elected": DURABLE,
    "decision.held": DURABLE,
    "decision.recorded": DURABLE,
    "decision.standing": DURABLE,
    "decision.result": DURABLE,
    "outside.recorded": DURABLE,
    GUIDANCE_KIND: DURABLE,
    "review.recorded": DURABLE,
    "utterance.captured": DURABLE,
    "session.opened": EPHEMERAL,
    "session.closed": EPHEMERAL,
    "loop.managed": DURABLE,
    "loop.intent": DURABLE,
    "loop.done": DURABLE,
    "loop.closed": DURABLE,
    SYSTEM_CLAIM_KIND: DURABLE,
}


def durability(kind: str) -> str:
    """``EPHEMERAL`` only for a kind declared so; anything else is durable."""
    return EPHEMERAL if DURABILITY.get(kind) == EPHEMERAL else DURABLE


def is_ephemeral(kind: str) -> bool:
    return durability(kind) == EPHEMERAL


class ValidationError(ValueError):
    """Unknown kind, unknown key, or a payload that fails its schema."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _fail(reason: str) -> None:
    raise ValidationError(reason)


# The refusal a write gets when text a picker displays breaks the displayed
# text rule (display_text_problem). ``ask_control_character:<field>`` is its
# reason for every class the rule refuses, so a caller compares one string;
# ``<field>`` names the payload field (``question_id``, ``options[1].label``).
ASK_CONTROL_CHARACTER = "ask_control_character"

# Text a picker displays, per kind, as (field, is the question body). Only
# the question body may hold a newline (a long question can run to several
# lines, and a line feed alone cannot move the cursor back over text). The
# header is ``ask K``, built from the event id, so no payload field holds it.
_DISPLAYED_TEXT: dict[str, tuple[tuple[str, bool], ...]] = {
    "decision.asked": (("question", True), ("question_id", False), ("scope", False)),
    "decision.elected": (
        ("question", True),
        ("question_id", False),
        ("scope", False),
        ("chosen", False),
        ("chosen_description", False),
    ),
}

# The displayed text rule reads three Unicode tables written out below, because
# Python's unicodedata has none of their properties: WHITE_SPACE_CODE_POINTS,
# BLANK_CODE_POINTS and DEFAULT_IGNORABLE_RANGES. Each holds the Unicode
# 16.0.0 data, and the rule reads it the same way on every interpreter whose
# database UNICODE_TABLE_VERSIONS names (Python 3.11 carries 14.0.0, 3.12
# 15.0.0, 3.13 15.1.0 and 3.14 16.0.0): a code point a table holds is decided
# by the table, never by that interpreter's own database.
#
# Update rule: test_ask_display_text_rule.py fails on an interpreter whose
# unicodedata.unidata_version is not in UNICODE_TABLE_VERSIONS. On a Unicode
# version bump, run
#     python3 -m pytest -q -p no:cacheprovider ledger/tests/test_ask_display_text_rule.py -k unicode_table
# on the new interpreter. Then re-derive each table from that version's
# Unicode Character Database (https://www.unicode.org/Public/<version>/ucd/):
# White_Space from PropList.txt, Default_Ignorable_Code_Point from
# DerivedCoreProperties.txt, and the blank table from its two sources as the
# comment on BLANK_CODE_POINTS says (UnicodeData.txt names, and the no-ink
# drawing probe; the test prints every BLANK or FILLER name it does not know).
# Update a table, the literal copy the test pins it to, and the version list
# together.
UNICODE_TABLE_VERSIONS = ("14.0.0", "15.0.0", "15.1.0", "16.0.0")

# Unicode White_Space (PropList.txt): the 25 code points the property names,
# the same in every version from 6.3.0 (which took U+180E out) to 16.0.0.
# str.isspace() is another set (it adds U+001C-U+001F), so the set is written
# out, and every interpreter decides these code points from this table, never
# from its own database. Displayed text holds none of them but U+0020 (and a
# newline inside the question).
WHITE_SPACE_CODE_POINTS = frozenset({
    0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x0020, 0x0085, 0x00A0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008,
    0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
})

# Code points that render as blank space but are not White_Space, from two
# sources.
#
# 1. Names: the characters whose name in UnicodeData.txt holds the word BLANK
#    or FILLER, less the ones whose glyph is a visible mark: U+2422 BLANK
#    SYMBOL and the scribal gap and line fillers (U+A8F9, U+10AF6, U+1144E,
#    U+11945, U+11F48). That leaves the Hangul fillers U+115F, U+1160, U+3164
#    and U+FFA0 (default ignorable too), U+2800 BRAILLE PATTERN BLANK, U+13441
#    EGYPTIAN HIEROGLYPH FULL BLANK and U+13442 EGYPTIAN HIEROGLYPH HALF BLANK
#    (which Unicode says are rendered as whitespace), and U+16FE4 KHITAN SMALL
#    SCRIPT FILLER (it marks an empty place in a Khitan cluster and draws
#    nothing).
# 2. No ink: code points the rule otherwise allows that a fallback font draws
#    with no ink. U+FFFC OBJECT REPLACEMENT CHARACTER (Menlo, the macOS
#    terminal font CoreText falls back to for it, draws it as a full-width
#    empty cell) and U+1D159 MUSICAL SYMBOL NULL NOTEHEAD (a notehead
#    placeholder that carries no ink of its own).
#
# Update rule for source 2, on a Unicode version bump and whenever a reviewer
# reports a blank-looking character: list every code point the rule allows
# (display_text_problem(f"a{c}b") or the question form is None), draw each one
# alone with the platform's text renderer and its font fallback (on macOS,
# CoreText starting from Helvetica and again from Menlo) into a bitmap, and
# count the inked pixels; draw each allowed combining mark on a base letter
# and compare with the letter alone. Every code point that leaves no ink,
# other than U+0020 and U+000A, goes into this table and the test's copy.
BLANK_CODE_POINTS = frozenset({
    0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0, 0xFFFC, 0x13441, 0x13442, 0x16FE4,
    0x1D159,
})

# Unicode Default_Ignorable_Code_Point (DerivedCoreProperties.txt) as
# inclusive ranges: code points a renderer may draw as nothing, so text that
# holds one looks the same as text without it. It is the same list in
# Unicode 14.0.0 to 16.0.0.
DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_COMBINING_MARKS = ("Mn", "Mc", "Me")


def control_character(text: str, *, allow_newline: bool = False) -> str | None:
    """``U+XXXX`` of the first control character in ``text``, else None.

    A control character is one of C0 (U+0000-U+001F, so ESC and every ANSI
    escape), DEL (U+007F) or C1 (U+0080-U+009F): Unicode category ``Cc`` and
    nothing else. ``allow_newline`` lets U+000A pass. A document's title and
    stored path use this definition; the text an ask displays uses the
    wider ``display_text_problem``. It is written as a code point so a
    message that names it never carries the character itself.
    """
    for char in text:
        if allow_newline and char == "\n":
            continue
        if unicodedata.category(char) == "Cc":
            return f"U+{ord(char):04X}"
    return None


def _code_point(char: str) -> str:
    return f"U+{ord(char):04X}"


def _default_ignorable(code: int) -> bool:
    return any(low <= code <= high for low, high in DEFAULT_IGNORABLE_RANGES)


def _white_space_class(code: int) -> str:
    """The class named for a White_Space code point, from the code point
    alone, so it is the same on every interpreter."""
    if code in (0x2028, 0x2029):
        return "a line or paragraph separator"
    if code <= 0x000D or code == 0x0085:
        return "a control character"
    return "a space other than U+0020"


def _refused_code_point(char: str, *, question: bool) -> str | None:
    """The class that refuses ``char`` anywhere in displayed text, else
    None. The two pinned tables decide first, so a White_Space or blank code
    point gets one answer on every interpreter whatever its database holds;
    a category this rule does not name is refused, never let through."""
    code = ord(char)
    if char == " " or (question and char == "\n"):
        return None
    if code in WHITE_SPACE_CODE_POINTS:
        return _white_space_class(code)
    if code in BLANK_CODE_POINTS:
        return "a blank character"
    category = unicodedata.category(char)
    if category == "Cc":
        return "a control character"
    if category == "Cf":
        return "a format character"
    if category == "Cs":
        return "a surrogate"
    if category == "Co":
        return "a private-use character"
    if category == "Cn":
        return "an unassigned character"
    if _default_ignorable(code):
        return "an invisible character"
    if category[:1] not in ("L", "M", "N", "P", "S"):
        return "a character of unknown category"
    return None


def _edge_space(char: str) -> bool:
    return unicodedata.category(char).startswith("Z") or char in "\t\n"


def _nfc_culprit(text: str) -> str:
    """The code point to name when ``text`` is not NFC: the first one at or
    after the first place NFC changes that NFC changes alone or that
    combines, else the code point at that place."""
    normal = unicodedata.normalize("NFC", text)
    start = next(
        (i for i, (a, b) in enumerate(zip(text, normal)) if a != b),
        min(len(text), len(normal)),
    )
    for char in text[start:]:
        if unicodedata.combining(char) or unicodedata.normalize("NFC", char) != char:
            return char
    return text[min(start, len(text) - 1)]


def display_text_problem(text: str, *, question: bool = False) -> tuple[str, str] | None:
    """``(class, U+XXXX)`` of the first reason ``text`` cannot be shown by a
    picker as it is stored, else None. ``question`` is True for the question
    body only.

    Displayed text holds no White_Space code point but U+0020
    (WHITE_SPACE_CODE_POINTS; a newline is allowed inside the question), no
    blank (BLANK_CODE_POINTS: U+2800, the Hangul fillers, the Egyptian
    hieroglyph blanks, the Khitan filler, U+FFFC and U+1D159), no code point
    of category Cc,
    Cf (the bidi embeddings, overrides and isolates, the zero-width space and
    joiners), Cs, Co or Cn, and no Default_Ignorable_Code_Point
    (DEFAULT_IGNORABLE_RANGES, which adds the variation selectors such as
    U+FE0F and U+034F). There is no emoji exception: U+200D and U+FE0F are
    refused in the question too. It does not start or end with whitespace
    (any Z* code point, a tab or a newline), does not start with a combining
    mark, and is NFC, so one visible string has one stored spelling. The
    class is a phrase for a message (``a format character``, ``an invisible
    character``, ``text that is not NFC``); the code point is written as
    ``U+XXXX`` so a message that names it never carries it.
    """
    for char in text:
        found = _refused_code_point(char, question=question)
        if found is not None:
            return found, _code_point(char)
    if text:
        if _edge_space(text[0]):
            return "leading whitespace", _code_point(text[0])
        if _edge_space(text[-1]):
            return "trailing whitespace", _code_point(text[-1])
        if unicodedata.category(text[0]) in _COMBINING_MARKS:
            return "a leading combining mark", _code_point(text[0])
    if not unicodedata.is_normalized("NFC", text):
        return "text that is not NFC", _code_point(_nfc_culprit(text))
    return None


def doc_path_problem(value: Any) -> str | None:
    """Why ``value`` is not a stored document path, else None.

    A stored path is a string whose UTF-8 encoding is 1 to
    ``DOC_PATH_MAX_BYTES`` bytes, made of components separated by single
    ``/``, each 1 to ``DOC_PATH_COMPONENT_MAX_BYTES`` bytes, none empty (so no
    leading, trailing or doubled ``/``, and never absolute), none ``.`` and
    none ``..``, with no control character anywhere (``control_character``,
    which covers NUL). It is relative to the database's document root, and
    one file has one stored spelling: ``doc add`` stores each component only
    as its directory's entry list spells it, refusing any other spelling a
    case-insensitive volume would open (``ledger/verbs/doc.py``).
    """
    if type(value) is not str:
        return "not a string"
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return "not UTF-8 text"
    if not encoded:
        return "empty"
    if len(encoded) > DOC_PATH_MAX_BYTES:
        return f"over {DOC_PATH_MAX_BYTES} bytes"
    if control_character(value) is not None:
        return "holds a control character"
    for component in value.split("/"):
        if component == "":
            return "has an empty component (a leading, trailing or doubled '/')"
        if component in (".", ".."):
            return f"has a '{component}' component"
        if len(component.encode("utf-8")) > DOC_PATH_COMPONENT_MAX_BYTES:
            return f"has a component over {DOC_PATH_COMPONENT_MAX_BYTES} bytes"
    return None


def display_control_character(
        kind: str, payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """``(field, class, code point)`` of the first ``display_text_problem``
    in the text a picker displays for a ``decision.asked`` or
    ``decision.elected`` payload, else None. A value that is not a string is
    skipped here: the type checks (write time: the schema; read time:
    ``render_ask_display``) own it.
    """
    for name, is_question in _DISPLAYED_TEXT.get(kind, ()):
        value = payload.get(name)
        if isinstance(value, str):
            found = display_text_problem(value, question=is_question)
            if found is not None:
                return (name, *found)
    if kind == "decision.asked":
        options = payload.get("options")
        for index, option in enumerate(options if isinstance(options, list) else []):
            if not isinstance(option, dict):
                continue
            for name in ("label", "description"):
                value = option.get(name)
                if isinstance(value, str):
                    found = display_text_problem(value)
                    if found is not None:
                        return (f"options[{index}].{name}", *found)
    elif kind == "decision.elected":
        rejected = payload.get("rejected")
        for index, value in enumerate(rejected if isinstance(rejected, list) else []):
            if isinstance(value, str):
                found = display_text_problem(value)
                if found is not None:
                    return (f"rejected[{index}]", *found)
    return None


def refuse_display_control_characters(kind: str, payload: Mapping[str, Any]) -> None:
    """Write-time rule: refuse an ask or election whose displayed text breaks
    ``display_text_problem`` (a control, format, separator, invisible or
    blank code point, text that is not NFC, and the rest), always with
    ``ask_control_character:<field>``. Not part of ``validate_payload``:
    verify, export and restore re-validate rows written before this rule,
    and those must still read; ``render_ask_display`` makes such an ask
    unpickable instead."""
    hit = display_control_character(kind, payload)
    if hit is not None:
        _fail(f"{ASK_CONTROL_CHARACTER}:{hit[0]}")


def known_kinds() -> tuple[str, ...]:
    return tuple(sorted({kind for kind, _version in SCHEMAS}))


def schema_for(kind: str, payload_version: int) -> dict[str, Any]:
    schema = SCHEMAS.get((kind, payload_version))
    if schema is None:
        if any(k == kind for k, _v in SCHEMAS):
            _fail(f"unknown-version:{kind}:{payload_version}")
        _fail(f"unknown-kind:{kind}")
    return schema


def _is_json(value: Any) -> bool:
    """True for a JSON value: null, a string, a boolean, an integer, a finite
    number, or an array or string-keyed object of JSON values."""
    try:
        if value is None or type(value) in (str, bool, int):
            return True
        if type(value) is float:
            return math.isfinite(value)
        if type(value) is list:
            return all(_is_json(item) for item in value)
        if type(value) is dict:
            return all(type(key) is str and _is_json(item) for key, item in value.items())
    except RecursionError:
        return False
    return False


def _field_error(reason: str, path: str, spec: Mapping[str, Any]) -> str:
    message = f"{reason}:{path}"
    if spec.get("show_allowed"):
        message += f"; allowed: {', '.join(spec['enum'])}"
    return message


def _check_field(value: Any, spec: Mapping[str, Any], path: str) -> None:
    if "enum" in spec:
        if not isinstance(value, str) or value not in spec["enum"]:
            _fail(_field_error("bad-type", path, spec))
        return
    field_type = spec["type"]
    if field_type == "string":
        if type(value) is not str:
            _fail(f"bad-type:{path}")
        min_len = spec.get("min_len", 0)
        if min_len and len(value) < min_len:
            _fail(f"bad-type:{path}")
        max_len = spec.get("max_len")
        if max_len is not None and len(value) > max_len:
            _fail(f"too-long:{path}")
        if spec.get("single_line") and ("\n" in value or "\r" in value):
            _fail(f"not-single-line:{path}")
        if spec.get("no_control") and control_character(value) is not None:
            _fail(f"{ASK_CONTROL_CHARACTER}:{path}")
        if spec.get("doc_path") and doc_path_problem(value) is not None:
            _fail(f"{DOC_PATH_REFUSAL}:{path}")
        pattern = spec.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            _fail(f"bad-type:{path}")
        if spec.get("calendar_stamp"):
            from . import lease

            try:
                lease.parse_stamp(value)
            except ValueError:
                _fail(f"bad-type:{path}")
        return
    if field_type == "integer":
        if type(value) is not int:
            _fail(f"bad-type:{path}")
        minimum = spec.get("min")
        if minimum is not None and value < minimum:
            _fail(f"bad-type:{path}")
        maximum = spec.get("max")
        if maximum is not None and value > maximum:
            _fail(f"too-large:{path}")
        return
    if field_type == "array":
        if type(value) is not list:
            _fail(f"bad-type:{path}")
        min_len = spec.get("min_len", 0)
        if min_len and len(value) < min_len:
            _fail(f"bad-type:{path}")
        max_len = spec.get("max_len")
        if max_len is not None and len(value) > max_len:
            _fail(f"too-long:{path}")
        item_spec = spec["items"]
        for index, item in enumerate(value):
            _check_field(item, item_spec, f"{path}[{index}]")
        return
    if field_type == "object":
        if type(value) is not dict:
            _fail(f"bad-type:{path}")
        min_keys = spec.get("min_keys", 0)
        if min_keys and len(value) < min_keys:
            _fail(f"bad-type:{path}")
        _check_object(value, spec.get("required", {}), spec.get("optional", {}), path)
        return
    if field_type == "boolean":
        if type(value) is not bool:
            _fail(f"bad-type:{path}")
        if spec.get("only_true") and value is not True:
            _fail(f"bad-type:{path}")
        return
    if field_type == "json_object":
        # Any JSON object, kept verbatim: data whose keys the ledger does not own.
        if type(value) is not dict or not _is_json(value):
            _fail(f"bad-type:{path}")
        return
    _fail(f"bad-type:{path}")


def _check_object(
    obj: Mapping[str, Any],
    required: Mapping[str, Any],
    optional: Mapping[str, Any],
    path: str,
) -> None:
    allowed = set(required) | set(optional)
    actual = set(obj)
    extra = actual - allowed
    if extra:
        _fail(f"unknown-key:{path}.{sorted(extra)[0]}" if path else f"unknown-key:{sorted(extra)[0]}")
    missing = set(required) - actual
    if missing:
        key = sorted(missing)[0]
        field_path = f"{path}.{key}" if path else key
        _fail(_field_error("missing-key", field_path, required[key]))
    for key, spec in required.items():
        _check_field(obj[key], spec, f"{path}.{key}" if path else key)
    for key, spec in optional.items():
        if key in obj:
            _check_field(obj[key], spec, f"{path}.{key}" if path else key)


def strip_directory_ref_slash(kind: str, payload: Any, payload_version: int = 1) -> Any:
    """``payload`` with one trailing slash stripped from each directory ref.

    The writing verbs call this before validation, so ``some/dir/`` in a
    ``refs`` list is stored as ``some/dir``. Only a ref that is a directory
    path once the slash is gone is changed; every other value, kind and
    payload is returned as given, and validation then refuses what it refused
    before.
    """
    schema = SCHEMAS.get((kind, payload_version))
    if schema is None or type(payload) is not dict:
        return payload
    out = payload
    for key, spec in (*schema["required"].items(), *schema["optional"].items()):
        value = payload.get(key)
        if spec.get("type") != "array" or spec.get("items") is not _REF_TARGET:
            continue
        if type(value) is not list:
            continue
        refs = [
            ref[:-1]
            if type(ref) is str and ref.endswith("/") and _DIRECTORY_REF.fullmatch(ref[:-1])
            else ref
            for ref in value
        ]
        if refs != value:
            out = {**out, key: refs}
    return out


def validate_payload(kind: str, payload: Any, payload_version: int = 1) -> dict[str, Any]:
    """Refuse unknown kinds and unknown keys; return ``payload`` when valid."""
    schema = schema_for(kind, payload_version)
    if type(payload) is not dict:
        _fail("bad-type:payload")
    _check_object(payload, schema["required"], schema["optional"], "")
    # Keys that must appear together or not at all.
    for group in schema.get("together", ()):
        present = [key for key in group if key in payload]
        if present and len(present) != len(group):
            _fail(f"incomplete-group:{','.join(group)}")
    # Keys of which exactly one appears: both and neither are refused.
    for group in schema.get("exactly_one_of", ()):
        present = [key for key in group if key in payload]
        if len(present) != 1:
            _fail(f"exactly-one-of:{','.join(group)}")
    return payload


def validate_item_id(kind: str, item_id: str | None, payload_version: int = 1) -> str | None:
    schema = schema_for(kind, payload_version)
    mode = schema["item_id"]
    if item_id is not None and type(item_id) is not str:
        _fail("bad-type:item_id")
    if mode == "absent":
        if item_id:
            _fail("unexpected-key:item_id")
        return None
    if mode == "required":
        if not item_id:
            _fail("missing-key:item_id")
        return item_id
    if not item_id:
        return None
    return item_id
