#!/usr/bin/env python3
"""Rebuild the forecast ledger from committed histories and compare WIS.

    python3 notebooks/recompute_forecast_wis.py --check
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from processors.forecast_ledger import build_reading  # noqa: E402

LATEST = ROOT / "readings" / "forecast-ledger-latest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rebuilt = build_reading(ROOT / "readings")
    if not LATEST.is_file():
        print("forecast-ledger-latest.json is missing")
        return 1 if args.check else 0
    published = json.loads(LATEST.read_text(encoding="utf-8"))
    keys = (
        "n_signals_scored",
        "n_forecasts",
        "pooled_empirical_coverage",
        "nominal_coverage",
        "n_beating_baseline",
    )
    mismatches = {
        key: {"published": published.get(key), "recomputed": rebuilt.get(key)}
        for key in keys
        if published.get(key) != rebuilt.get(key)
    }
    report = {
        "published_at": published.get("generated_at"),
        "recomputed_headline": rebuilt.get("headline"),
        "n_mismatch_fields": len(mismatches),
        "mismatches": mismatches,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
