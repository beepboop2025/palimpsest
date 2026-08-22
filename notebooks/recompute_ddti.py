#!/usr/bin/env python3
"""Recompute DDTI threat identities from the sealed latest file.

threat = attention * (1 + NOVELTY_WEIGHT * novelty) with NOVELTY_WEIGHT = 1.5,
the same formula as processors/ddti_index.combine_threat. This script does not
rebuild attention from the CDT feed; it checks that the published ranked rows
still obey the identity they claim.

    python3 notebooks/recompute_ddti.py
    python3 notebooks/recompute_ddti.py --check
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LATEST = ROOT / "readings" / "ddti-latest.json"
NOVELTY_WEIGHT = 1.5
# Published threat scores are rounded; 4-decimal identity is the public claim.
TOLERANCE = 5e-4


def recompute(path: Path = LATEST) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    ranked = document.get("ranked") or []
    mismatches = []
    checked = 0
    for row in ranked:
        if not isinstance(row, dict):
            continue
        attention = row.get("attention")
        novelty = row.get("novelty")
        threat = row.get("threat")
        if not all(isinstance(value, (int, float)) for value in (attention, novelty, threat)):
            continue
        expected = float(attention) * (1.0 + NOVELTY_WEIGHT * float(novelty))
        checked += 1
        if abs(expected - float(threat)) > TOLERANCE:
            mismatches.append(
                {
                    "term": row.get("term"),
                    "published": threat,
                    "recomputed": round(expected, 6),
                }
            )
    return {
        "path": str(path.relative_to(ROOT)),
        "generated_at": document.get("generated_at"),
        "n_ranked": len(ranked),
        "n_checked": checked,
        "n_mismatch": len(mismatches),
        "mismatches": mismatches[:12],
        "formula": "threat = attention * (1 + 1.5 * novelty)",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not LATEST.is_file():
        print("DDTI latest file is missing; nothing to recompute")
        return 1 if args.check else 0
    result = recompute()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["n_mismatch"]:
        return 1
    if result["n_checked"] == 0:
        print("no numeric threat rows to check")
        return 1 if args.check else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
