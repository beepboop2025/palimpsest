"""Verify the sealed erasure ledger — the "you can check us" tool.

Recomputes the entire hash chain and Merkle root of readings/erasure-ledger.jsonl
and reports every integrity break. Exit code 0 = intact, 1 = tampered/broken.
Anyone who cloned the repo can run this against any past commit.

    python3 scripts/verify_ledger.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import sealed_ledger  # noqa: E402
LEDGER = os.path.join(ROOT, "readings", "erasure-ledger.jsonl")


def main() -> int:
    entries = sealed_ledger.read_ledger(LEDGER)
    if not entries:
        print("ledger is empty — nothing to verify")
        return 0
    ok, problems = sealed_ledger.verify(entries)
    root = sealed_ledger.merkle_root(entries)
    print(f"entries      : {len(entries)}")
    print(f"first sealed : {entries[0]['ts']}")
    print(f"head sealed  : {entries[-1]['ts']}")
    print(f"head hash    : {entries[-1]['entry_hash']}")
    print(f"merkle root  : {root}")
    if ok:
        print("STATUS       : INTACT — every entry recomputes, chain and order verified")
        return 0
    print("STATUS       : BROKEN — the record was altered after sealing:")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
