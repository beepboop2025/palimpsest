"""Verify the Verifiable Eval Registry — recompute the chain and enforce that every
result was pre-registered before it was run. Exit 0 = intact, 1 = broken.

    python3 scripts/verify_eval_registry.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import eval_registry as reg  # noqa: E402

REGISTRY = os.path.join(ROOT, "readings", "eval-registry.jsonl")


def main() -> int:
    entries = reg.read_ledger(REGISTRY)
    if not entries:
        print("registry is empty — nothing to verify")
        return 0
    ok, problems = reg.verify(entries)
    s = reg.summary(REGISTRY)
    print(f"attestations : {s['attestations']} ({s['preregistrations']} preregistered, {s['runs']} runs)")
    print(f"models       : {', '.join(s['models']) or '-'}")
    print(f"merkle root  : {s['merkle_root']}")
    print(f"head hash    : {s['head_hash']}")
    if ok:
        print("STATUS       : INTACT — chain verifies and every run was pre-registered first")
        return 0
    print("STATUS       : BROKEN:")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
