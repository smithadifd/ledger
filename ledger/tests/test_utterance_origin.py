"""utterance.captured: origin, and the oversize-prompt pointer pair."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger import ValidationError
from ledger.store import open_ledger
from test_elect import _db, append_elected, elect_verb, invoke, lint, seed_item

SHA = "a" * 64


def _append(tmp_path: Path, extra: dict, n: int = 0):
    lg = open_ledger(_db(tmp_path))
    return lg.append(
        kind="utterance.captured",
        payload={"text": "hello", **extra},
        idempotency_key=f"k-{n}",
    )


def test_origin_person_accepted(tmp_path: Path) -> None:
    assert _append(tmp_path, {"origin": "person"}).id


def test_origin_absent_accepted(tmp_path: Path) -> None:
    assert _append(tmp_path, {}).id


def test_pointer_pair_accepted(tmp_path: Path) -> None:
    ev = _append(tmp_path, {"origin": "person", "omitted_bytes": 9000, "omitted_sha256": SHA})
    assert ev.id


@pytest.mark.parametrize(
    "extra, code",
    [
        ({"origin": "agent"}, "bad-type:origin"),
        ({"omitted_bytes": 5}, "incomplete-group:omitted_bytes,omitted_sha256"),
        ({"omitted_sha256": SHA}, "incomplete-group:omitted_bytes,omitted_sha256"),
        ({"omitted_bytes": 5, "omitted_sha256": "a" * 63}, "bad-type:omitted_sha256"),
        ({"omitted_bytes": 5, "omitted_sha256": "a" * 65}, "bad-type:omitted_sha256"),
        ({"omitted_bytes": 5, "omitted_sha256": "A" * 64}, "bad-type:omitted_sha256"),
        ({"omitted_bytes": 0, "omitted_sha256": SHA}, "bad-type:omitted_bytes"),
    ],
)
def test_refusals(tmp_path: Path, extra: dict, code: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _append(tmp_path, extra)
    assert exc.value.reason == code


def test_cli_append_accepts_new_keys(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    payload = json.dumps(
        {"text": "[omitted]", "origin": "person", "omitted_bytes": 9000, "omitted_sha256": SHA}
    )
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", db, "append", "--kind", "utterance.captured",
                      "--payload", payload, "--channel", "terminal",
                      "--provenance-class", "submitted-prompt"],
    )
    assert code == 0, err


def test_elect_refuses_pointer_utterance(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    payload = json.dumps({"text": "[omitted]", "omitted_bytes": 9000, "omitted_sha256": SHA})
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", db, "append", "--kind", "utterance.captured",
                      "--payload", payload, "--channel", "terminal",
                      "--provenance-class", "submitted-prompt"],
    )
    assert code == 0, err
    ref = json.loads(out)["data"]["event"]["id"]
    code, out, err = elect_verb(monkeypatch, db, "it-1", ref, quotes=["[omitted]"])
    assert code != 0
    assert json.loads(out)["error"]["code"] == "pointer_utterance"


def test_lint_quotes_refuses_election_on_pointer_utterance(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    payload = json.dumps({"text": "[omitted]", "omitted_bytes": 9000, "omitted_sha256": SHA})
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", db, "append", "--kind", "utterance.captured",
                      "--payload", payload, "--channel", "terminal",
                      "--provenance-class", "submitted-prompt"],
    )
    assert code == 0, err
    ref = json.loads(out)["data"]["event"]["id"]
    code, out, err = append_elected(monkeypatch, db, "it-1", ref, quotes=["[omitted]"])
    assert code == 0, err
    event_id = json.loads(out)["data"]["event"]["id"]
    code, out, err = lint(monkeypatch, db)
    assert code != 0, err
    body = json.loads(out)
    assert body["error"]["code"] == "quote_mismatch"
    assert f"event={event_id} quote=None: quote_ref is an omitted-body pointer" in body["error"]["message"]
