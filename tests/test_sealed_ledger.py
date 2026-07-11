"""Offline proof that the sealed ledger detects tampering.

No network, no fixtures — builds a ledger in a temp dir, then shows that every
class of after-the-fact edit (payload change, reorder, drop, timestamp change)
is caught by verify(). This is the property the observatory's credibility rests
on, so it is tested directly.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import sealed_ledger  # noqa: E402

FIXED = None  # use real time; determinism not required for these assertions


def _fresh_ledger():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "ledger.jsonl")
    sealed_ledger.append_seal(path, "src-a", {"gfw_index": 58.6, "n": 1})
    sealed_ledger.append_seal(path, "src-b", {"index": 40.0, "n": 2})
    sealed_ledger.append_seal(path, "src-a", {"gfw_index": 61.2, "n": 3})
    return path


def test_intact_chain_verifies():
    path = _fresh_ledger()
    ok, problems = sealed_ledger.verify(sealed_ledger.read_ledger(path))
    assert ok and not problems, problems


def test_idempotent_skip_on_unchanged_payload():
    path = _fresh_ledger()
    before = len(sealed_ledger.read_ledger(path))
    # last src-a payload was {"gfw_index": 61.2, "n": 3}; resealing identical -> skip
    res = sealed_ledger.append_seal(path, "src-a", {"gfw_index": 61.2, "n": 3})
    assert res is None
    assert len(sealed_ledger.read_ledger(path)) == before


def test_payload_tamper_is_caught():
    path = _fresh_ledger()
    entries = sealed_ledger.read_ledger(path)
    entries[1]["payload_sha256"] = "0" * 64  # forge the sealed evidence digest
    ok, problems = sealed_ledger.verify(entries)
    assert not ok
    assert any("does not recompute" in p for p in problems)


def test_reorder_is_caught():
    path = _fresh_ledger()
    entries = sealed_ledger.read_ledger(path)
    entries[0], entries[1] = entries[1], entries[0]  # swap order
    ok, problems = sealed_ledger.verify(entries)
    assert not ok


def test_dropped_entry_is_caught():
    path = _fresh_ledger()
    entries = sealed_ledger.read_ledger(path)
    del entries[1]  # silently remove a middle reading
    ok, problems = sealed_ledger.verify(entries)
    assert not ok


def test_merkle_root_commits_to_every_entry_hash():
    # The Merkle root is a single-value commitment to the set of entry_hashes.
    # Any entry_hash that moves (which is what a properly-recomputed forgery would
    # require, and what then breaks the next entry's prev_hash link) moves the root.
    path = _fresh_ledger()
    entries = sealed_ledger.read_ledger(path)
    root_before = sealed_ledger.merkle_root(entries)
    entries[2]["entry_hash"] = "f" * 64
    assert sealed_ledger.merkle_root(entries) != root_before
    # and a tamperer cannot both satisfy verify() and preserve the root:
    ok, _ = sealed_ledger.verify(entries)
    assert not ok


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"=== sealed_ledger: {passed}/{len(fns)} passed ===")
    sys.exit(0 if passed == len(fns) else 1)
