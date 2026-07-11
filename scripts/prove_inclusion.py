"""Produce (or check) a Merkle inclusion proof for one sealed attestation.

A proof lets a third party confirm that one specific entry is inside the
published chain, against the published Merkle root, with log2(N) hashes —
no need to download or trust the rest of the ledger.

    python3 scripts/prove_inclusion.py <seq>                    # eval registry
    python3 scripts/prove_inclusion.py <seq> --chain erasure    # erasure ledger
    python3 scripts/prove_inclusion.py --check proof.json       # verify a proof

The proof is self-contained JSON. To verify one anywhere, fold the path:
start from entry_hash, at each step sha256(left + right) with the sibling on
the stated side, and compare the result to merkle_root. That is all
--check does; it needs nothing else from this repository.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.sealed_ledger import (inclusion_proof, read_ledger,  # noqa: E402
                                verify_inclusion)

CHAINS = {
    "registry": os.path.join(ROOT, "readings", "eval-registry.jsonl"),
    "erasure": os.path.join(ROOT, "readings", "erasure-ledger.jsonl"),
}


def main(argv: list[str]) -> int:
    if "--check" in argv:
        path = argv[argv.index("--check") + 1]
        with open(path, encoding="utf-8") as f:
            proof = json.load(f)
        ok = verify_inclusion(proof)
        print(f"seq {proof.get('seq')} against root {proof.get('merkle_root', '')[:16]}… : "
              + ("VALID — entry is in the sealed chain" if ok else "INVALID — proof does not fold to the root"))
        return 0 if ok else 1

    chain = "registry"
    if "--chain" in argv:
        chain = argv[argv.index("--chain") + 1]
        if chain not in CHAINS:
            print(f"unknown chain {chain!r}; pick one of {', '.join(CHAINS)}")
            return 2
    seqs = [a for a in argv if a.isdigit()]
    if not seqs:
        print(__doc__)
        return 2
    entries = read_ledger(CHAINS[chain])
    proof = inclusion_proof(entries, int(seqs[0]))
    assert verify_inclusion(proof), "freshly generated proof must verify"
    print(json.dumps(proof, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
