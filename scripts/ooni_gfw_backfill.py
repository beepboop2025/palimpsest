"""One-shot backfill for the OONI GFW signal's history — give the chart depth
without waiting weeks for it to accrue.

Walks back N weekly windows, asks OONI's aggregation API for each, computes the
same measurement-weighted GFW index the live signal uses, and merges rows into
readings/ooni-gfw-history.jsonl (deduped by window). Run manually:

    PYTHONPATH=. python -m scripts.ooni_gfw_backfill 12

Vantage-insensitive, key-less, stdlib-only — same OONI open data as the live
pull, just over past windows.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from collectors.ooni_gfw import test_signals

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "readings", "ooni-gfw-history.jsonl")
WINDOW_DAYS = 7


def _index_for(since: str, until: str) -> dict | None:
    tests = test_signals(since, until, throttle=1.0)
    usable = [t for t in tests if t.get("available")]
    if not usable:
        return None
    tot_valid = sum((t["measurement_count"] - t["failure_count"]) for t in usable)
    tot_anom = sum(t["anomaly_count"] for t in usable)
    if tot_valid <= 0:
        return None
    return {
        "gfw_index": round(100 * tot_anom / tot_valid, 1),
        "n_measurements": sum(t["measurement_count"] for t in usable),
        "n_tests_with_data": len(usable),
    }


def main() -> None:
    weeks = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    now = datetime.now(timezone.utc)

    existing = {}
    if os.path.exists(HIST):
        for line in open(HIST, encoding="utf-8"):
            try:
                row = json.loads(line)
                key = (row.get("window_end") or row.get("generated_at", ""))[:10]
                existing[key] = row
            except ValueError:
                continue

    added = 0
    for w in range(1, weeks + 1):
        until_dt = now - timedelta(days=WINDOW_DAYS * (w - 1))
        since_dt = until_dt - timedelta(days=WINDOW_DAYS)
        since, until = since_dt.strftime("%Y-%m-%d"), until_dt.strftime("%Y-%m-%d")
        key = until[:10]
        if key in existing:
            continue
        idx = _index_for(since, until)
        if not idx:
            print(f"  {since}..{until}: no data, skipping")
            continue
        row = {
            "generated_at": until_dt.isoformat(),
            "window_start": since, "window_end": until,
            "backfilled": True, **idx,
        }
        existing[key] = row
        added += 1
        print(f"  {since}..{until}: index {idx['gfw_index']} "
              f"({idx['n_measurements']} measurements)")

    # rewrite history sorted by window end date
    rows = sorted(existing.values(),
                  key=lambda r: (r.get("window_end") or r.get("generated_at", ""))[:10])
    with open(HIST, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"=== backfill: added {added} weekly rows; history now {len(rows)} rows ===")


if __name__ == "__main__":
    main()
