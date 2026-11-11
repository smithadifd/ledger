"""Find known credential shapes in a document's bytes.

One scanner for every place a document's bytes are checked: ``doc add``
(before anything is written), restore and replay (before anything is
inserted), and ``ledger export`` (before any event file is written). It
reads bytes, not text, so a binary document is scanned too. A hit names the
shape and the byte offset where it starts, never the bytes that matched, so
no refusal built from it can print a credential.

The scan is a tripwire for a few common shapes, not a secret detector: a
credential in any other shape passes it.

The shapes:

- ``private-key``: five hyphens, ``BEGIN ``, zero or more words of uppercase
  letters each followed by one space, then ``PRIVATE KEY`` and five hyphens
  (RSA, EC, DSA, OpenSSH, encrypted and plain PKCS 8 headers).
- ``github-token``: ``ghp_``, ``gho_``, ``ghu_``, ``ghs_`` or ``ghr_`` and at
  least 36 ASCII letters or digits; or ``github_pat_`` and at least 22 ASCII
  letters, digits or underscores.
- ``aws-access-key``: ``AKIA`` or ``ASIA`` and exactly 16 uppercase ASCII
  letters or digits, with no further letter or digit after them.
- ``openai-key``: ``sk-`` and at least 20 ASCII letters, digits, hyphens or
  underscores.
- ``slack-token``: ``xox``, one of ``a``, ``b``, ``p``, ``r`` or ``s``, a
  hyphen, and at least 10 ASCII letters, digits or hyphens.

Every shape but ``private-key`` is a token shape: it matches only at the
start of the bytes or after a byte that is not an ASCII letter, digit or
underscore, so a word such as ``risk-`` or ``task-`` followed by letters is
not read as the start of an ``sk-`` key.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A token shape starts at the beginning of the bytes or after a byte that is
# not an ASCII letter, digit or underscore.
_TOKEN_START = rb"(?<![A-Za-z0-9_])"

SHAPES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private-key", re.compile(rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    (
        "github-token",
        re.compile(
            _TOKEN_START + rb"(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22})"
        ),
    ),
    (
        "aws-access-key",
        re.compile(_TOKEN_START + rb"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"),
    ),
    ("openai-key", re.compile(_TOKEN_START + rb"sk-[A-Za-z0-9_-]{20}")),
    ("slack-token", re.compile(_TOKEN_START + rb"xox[abprs]-[A-Za-z0-9-]{10}")),
)
SHAPE_NAMES = tuple(name for name, _pattern in SHAPES)


@dataclass(frozen=True)
class Hit:
    """One shape found: its name and the byte offset of its first match."""

    shape: str
    offset: int


def scan(data: bytes) -> tuple[Hit, ...]:
    """Every listed shape found in ``data``, each once, at the offset of its
    first match, ordered by offset. Empty when none is found."""
    hits = []
    for name, pattern in SHAPES:
        match = pattern.search(data)
        if match is not None:
            hits.append(Hit(name, match.start()))
    return tuple(sorted(hits, key=lambda hit: (hit.offset, hit.shape)))


def describe(hits: tuple[Hit, ...]) -> str:
    """``hits`` as text: each shape name and its byte offset, never the
    matched bytes."""
    return ", ".join(f"{hit.shape} at byte {hit.offset}" for hit in hits)


def shape_names(hits: tuple[Hit, ...]) -> str:
    """The shape names in ``hits``, comma-separated."""
    return ", ".join(hit.shape for hit in hits)
