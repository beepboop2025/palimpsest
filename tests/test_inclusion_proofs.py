"""Offline proof that Merkle inclusion proofs work and catch forgery.

A proof must verify for every entry of every ledger size (odd and even leaf
counts exercise the duplicate-last padding), must match the published
merkle_root, and must fail if the entry, the path, or the root is doctored.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import sealed_ledger as led  # noqa: E402


def _ledger(n: int) -> list[dict]:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "ledger.jsonl")
    for i in range(n):
        led.append_seal(path, f"src-{i}", {"reading": i, "v": i * 1.5})
    return led.read_ledger(path)


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 8, 9])
def test_every_entry_proves_against_the_published_root(n):
    entries = _ledger(n)
    root = led.merkle_root(entries)
    for seq in range(n):
        proof = led.inclusion_proof(entries, seq)
        assert proof["merkle_root"] == root
        assert led.verify_inclusion(proof)


def test_tampered_entry_hash_fails():
    entries = _ledger(5)
    proof = led.inclusion_proof(entries, 2)
    proof["entry_hash"] = "f" * 64
    assert not led.verify_inclusion(proof)


def test_tampered_path_fails():
    entries = _ledger(5)
    proof = led.inclusion_proof(entries, 2)
    if proof["path"]:
        proof["path"][0]["hash"] = "f" * 64
        assert not led.verify_inclusion(proof)


def test_wrong_root_fails():
    entries = _ledger(5)
    proof = led.inclusion_proof(entries, 2)
    proof["merkle_root"] = "f" * 64
    assert not led.verify_inclusion(proof)


def test_proof_from_one_ledger_rejected_by_another():
    a, b = _ledger(4), _ledger(6)
    proof = led.inclusion_proof(a, 1)
    proof["merkle_root"] = led.merkle_root(b)
    assert not led.verify_inclusion(proof)


def test_out_of_range_seq_raises():
    entries = _ledger(3)
    with pytest.raises(ValueError):
        led.inclusion_proof(entries, 3)
    with pytest.raises(ValueError):
        led.inclusion_proof([], 0)
