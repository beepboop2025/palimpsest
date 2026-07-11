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
