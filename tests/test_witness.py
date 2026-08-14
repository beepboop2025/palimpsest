"""Offline proof that the independent witness catches what it exists to catch:
a history rewrite, a shrunk chain, and a chain that fails its own rules — and
stays quiet on an honest append. The witness is a separate implementation from
core/, so these tests also pin that its verifiers agree with the real chains
in readings/.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "palimpsest_witness", os.path.join(ROOT, "ops", "witness", "palimpsest_witness.py"))
witness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(witness)


def _load(name: str) -> list[dict]:
    with open(os.path.join(ROOT, "readings", name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_witness_verifiers_accept_the_real_published_chains():
    assert witness.verify_registry(_load("eval-registry.jsonl")) == []
    assert witness.verify_erasure(_load("erasure-ledger.jsonl")) == []


def test_witness_catches_a_rewritten_entry():
    entries = _load("eval-registry.jsonl")
    entries[0] = dict(entries[0], ts="1999-01-01T00:00:00+00:00")
    problems = witness.verify_registry(entries)
    assert any("does not recompute" in p for p in problems)


def test_witness_catches_answers_before_questions():
    entries = _load("eval-registry.jsonl")
    runs = [e for e in entries if e.get("kind") == "run"]
    fake = dict(runs[0], probe_set_hash="f" * 64)
    fake["entry_hash"] = witness._sha256(witness._canonical(
        {k: v for k, v in fake.items() if k != "entry_hash"}))
    # a syntactically valid run whose probe set was never frozen
    problems = witness.verify_registry(entries[:1] + [dict(fake, seq=1, prev_hash=entries[0]["entry_hash"])])
    assert any("never pre-registered" in p for p in problems)


def test_prefix_consistency_quiet_on_honest_append():
    entries = _load("eval-registry.jsonl")
    obs = [{"ts": "2026-07-11T00:00:00+00:00", "n": len(entries) - 2,
            "head": entries[-3]["entry_hash"]}]
    assert witness.prefix_alerts("eval-registry", entries, obs) == []


def test_prefix_consistency_catches_rewrite():
    entries = _load("eval-registry.jsonl")
    obs = [{"ts": "2026-07-11T00:00:00+00:00", "n": len(entries),
            "head": "f" * 64}]  # what a witness saw before the "rewrite"
    alerts = witness.prefix_alerts("eval-registry", entries, obs)
    assert len(alerts) == 1 and "REWRITTEN" in alerts[0]


def test_prefix_consistency_catches_shrunk_history():
    entries = _load("eval-registry.jsonl")
    obs = [{"ts": "2026-07-11T00:00:00+00:00", "n": len(entries) + 5,
            "head": "a" * 64}]
    alerts = witness.prefix_alerts("eval-registry", entries, obs)
    assert len(alerts) == 1 and "SHRANK" in alerts[0]


def test_witness_root_matches_core_root():
    from core import sealed_ledger as led
    entries = _load("eval-registry.jsonl")
    assert witness.merkle_root(entries) == led.merkle_root(entries)


class _ChainResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


def test_witness_bounds_chain_download_before_decoding(monkeypatch):
    monkeypatch.setattr(witness, "MAX_CHAIN_BYTES", 8)

    def opener(_request, timeout):
        assert timeout == 60
        return _ChainResponse(b"123456789")

    try:
        witness.fetch_chain("https://palimpsest.info/readings/test.jsonl", opener)
    except ValueError as exc:
        assert str(exc) == "published chain exceeds witness byte ceiling"
    else:
        raise AssertionError("oversized chain was accepted")


def test_public_witness_ages_bleedthrough_bytes_actually_served():
    problems = witness.verify_public_freshness(
        "bleedthrough",
        {
            "signal": "bleedthrough",
            "generated_at": "2026-08-13T08:00:00Z",
        },
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert [(item["condition"], item["state"]) for item in problems] == [
        ("artifact/bleedthrough", "stale")
    ]


def test_public_witness_checks_embedded_deadlines_without_alerting_disabled_or_absent_optional():
    document = {
        "schema_version": "osint-china.v1",
        "generated_at": "2026-08-14T11:55:00Z",
        "signals": [
            {
                "id": "bleedthrough",
                "status": "live",
                "optional": True,
                "source_timestamp": "2026-08-13T08:00:00Z",
                "freshness_deadline": "2026-08-13T22:00:00Z",
                "health": {"collector_status": None},
            },
            {
                "id": "baike-redaction",
                "status": "stale",
                "optional": True,
                "source_timestamp": "2026-07-30T00:00:00Z",
                "freshness_deadline": "2026-07-31T00:00:00Z",
                "health": {"collector_status": "disabled_no_authorized_access"},
            },
            {
                "id": "undeployed-optional",
                "status": "missing",
                "optional": True,
                "source_timestamp": None,
                "freshness_deadline": None,
                "health": {"collector_status": None},
            },
            {
                "id": "ddti",
                "status": "live",
                "optional": False,
                "source_timestamp": "2026-08-14T11:50:00Z",
                "freshness_deadline": "2026-08-14T13:00:00Z",
                "health": {"collector_status": None},
            },
        ],
    }
    problems = witness.verify_public_freshness(
        "osint-china",
        document,
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert [(item["condition"], item["state"]) for item in problems] == [
        ("osint/bleedthrough", "stale")
    ]


def test_public_witness_rejects_old_bundle_even_if_embedded_signal_is_live():
    document = {
        "schema_version": "osint-china.v1",
        "generated_at": "2026-08-14T08:00:00Z",
        "signals": [{
            "id": "ddti",
            "status": "live",
            "optional": False,
            "source_timestamp": "2026-08-14T11:50:00Z",
            "freshness_deadline": "2026-08-14T13:00:00Z",
            "health": {"collector_status": None},
        }],
    }
    problems = witness.verify_public_freshness(
        "osint-china",
        document,
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert any(
        item["condition"] == "artifact/osint-china" and item["state"] == "stale"
        for item in problems
    )


def test_public_witness_timer_meets_bundle_detection_window():
    root = os.path.join(ROOT, "ops", "witness")
    timer = open(os.path.join(root, "palimpsest-witness.timer"), encoding="utf-8").read()
    service = open(os.path.join(root, "palimpsest-witness.service"), encoding="utf-8").read()
    assert "OnCalendar=*:0/15" in timer
    assert "Persistent=true" in timer
    assert "TimeoutStartSec=3m" in service
    assert "EnvironmentFile=-/etc/palimpsest-witness.env" in service

    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    assert "systemd-analyze verify" in readme
    assert "/etc/systemd/system/palimpsest-witness.service" in readme
    assert "/etc/systemd/system/palimpsest-witness.timer" in readme
    assert "shared Palimpsest host" in readme


def test_public_freshness_latch_retries_configured_delivery_failure():
    opened = {"osint/bleedthrough"}
    assert not witness._should_latch_freshness(
        opened, alerting_configured=True, delivered=False
    )
    assert witness._should_latch_freshness(
        opened, alerting_configured=True, delivered=True
    )
    assert witness._should_latch_freshness(
        opened, alerting_configured=False, delivered=False
    )
