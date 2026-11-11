"""The credential-shape scanner (ledger/credscan.py).

Each sample is assembled from pieces at run time, so no complete credential
shape appears in this file's source. Every sample is synthetic.
"""
from __future__ import annotations

import pytest

from ledger import credscan

# One synthetic sample per shape, keyed by the shape's name.
SAMPLES: dict[str, bytes] = {
    "private-key": b"-----BEGIN " + b"RSA " + b"PRIVATE KEY-----",
    "github-token": b"gh" + b"p_" + b"A1b2C3d4" * 4 + b"E5f6",
    "aws-access-key": b"AK" + b"IA" + b"QWERTYUIOP234567",
    "openai-key": b"s" + b"k-" + b"proj_Abc-123_" + b"x" * 10,
    "slack-token": b"xo" + b"xb-" + b"1234567890-abcdef",
}

# Bytes that look close to a shape and must not match it.
NEAR_MISSES: dict[str, bytes] = {
    # a word ending in "sk", a hyphen and 20 or more letters: only the
    # left-boundary rule keeps it from reading as an "sk-" key
    "risk-word": b"ri" + b"sk-" + b"abcdefghijklmnopqrstuvwxyz",
    "aws-15": b"AK" + b"IA" + b"QWERTYUIOP23456",
    "github-35": b"gh" + b"p_" + b"A1b2C3d4" * 4 + b"E5f",
    "public-key": b"-----BEGIN " + b"PUBLIC KEY-----",
}


def wrap(sample: bytes) -> bytes:
    """``sample`` inside a document, away from its start."""
    return b"# Notes\n\nsee: " + sample + b"\n\nend\n"


@pytest.mark.parametrize("shape", sorted(SAMPLES))
def test_each_shape_is_found_by_name_at_its_offset(shape):
    data = wrap(SAMPLES[shape])
    assert credscan.scan(data) == (credscan.Hit(shape, data.index(SAMPLES[shape])),)


def test_the_shape_list_is_the_five_named_shapes():
    assert credscan.SHAPE_NAMES == (
        "private-key", "github-token", "aws-access-key", "openai-key", "slack-token",
    )
    assert set(SAMPLES) == set(credscan.SHAPE_NAMES)


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_near_misses_are_not_found(name):
    assert credscan.scan(wrap(NEAR_MISSES[name])) == ()


@pytest.mark.parametrize(
    "header",
    [
        b"RSA PRIVATE KEY",
        b"EC PRIVATE KEY",
        b"DSA PRIVATE KEY",
        b"OPENSSH PRIVATE KEY",
        b"ENCRYPTED PRIVATE KEY",
        b"PRIVATE KEY",
    ],
)
def test_every_private_key_header_is_found(header):
    data = b"-----BEGIN " + header + b"-----\n"
    assert credscan.scan(data) == (credscan.Hit("private-key", 0),)


@pytest.mark.parametrize(
    "token",
    [
        b"gh" + b"o_" + b"z" * 36,
        b"gh" + b"u_" + b"9" * 36,
        b"gh" + b"s_" + b"Q" * 40,
        b"gh" + b"r_" + b"a1" * 18,
        b"git" + b"hub_pat_" + b"A_b" * 8,
    ],
)
def test_every_github_prefix_is_found(token):
    assert credscan.scan(token) == (credscan.Hit("github-token", 0),)


@pytest.mark.parametrize(
    "token",
    [b"xo" + b"x" + letter + b"-" + b"0123456789" for letter in (b"a", b"b", b"p", b"r", b"s")],
)
def test_every_slack_letter_is_found(token):
    assert credscan.scan(token) == (credscan.Hit("slack-token", 0),)


def test_an_aws_key_needs_exactly_sixteen_and_no_letter_or_digit_after():
    key = b"AS" + b"IA" + b"ABCDEFGHIJKLMNOP"
    assert credscan.scan(key) == (credscan.Hit("aws-access-key", 0),)
    assert credscan.scan(key + b".") == (credscan.Hit("aws-access-key", 0),)
    for tail in (b"Q", b"q", b"7"):
        assert credscan.scan(key + tail) == (), tail
    # lowercase inside the sixteen is not the shape
    assert credscan.scan(b"AK" + b"IA" + b"abcdefghijklmnop") == ()


@pytest.mark.parametrize("before", [b"a", b"Z", b"0", b"_"])
def test_a_token_after_a_letter_digit_or_underscore_is_not_found(before):
    for shape in ("github-token", "aws-access-key", "openai-key", "slack-token"):
        assert credscan.scan(before + SAMPLES[shape]) == (), (shape, before)


@pytest.mark.parametrize("before", [b"", b" ", b"-", b"=", b"\x00", b"\xff", b"\n"])
def test_a_token_at_the_start_or_after_another_byte_is_found(before):
    for shape in ("github-token", "aws-access-key", "openai-key", "slack-token"):
        assert credscan.scan(before + SAMPLES[shape]) == (
            credscan.Hit(shape, len(before)),
        ), (shape, before)


def test_binary_bytes_are_scanned_and_hits_are_ordered_by_offset():
    data = (
        b"\x00\xff\xfe" + SAMPLES["slack-token"] + b"\x00"
        + SAMPLES["private-key"] + b"\xff" + SAMPLES["slack-token"]
    )
    hits = credscan.scan(data)
    assert hits == (
        credscan.Hit("slack-token", 3),
        credscan.Hit("private-key", data.index(SAMPLES["private-key"])),
    )


def test_describe_names_shapes_and_offsets_and_never_the_bytes():
    data = wrap(SAMPLES["openai-key"]) + SAMPLES["aws-access-key"]
    hits = credscan.scan(data)
    text = credscan.describe(hits)
    assert text == (
        f"openai-key at byte {data.index(SAMPLES['openai-key'])}, "
        f"aws-access-key at byte {data.index(SAMPLES['aws-access-key'])}"
    )
    for sample in SAMPLES.values():
        assert sample.decode() not in text
    assert credscan.shape_names(hits) == "openai-key, aws-access-key"


def test_clean_bytes_have_no_hit():
    assert credscan.scan(b"") == ()
    assert credscan.scan(b"# Brief\n\nTask-1: a risk-free plan, sk- alone.\n") == ()
