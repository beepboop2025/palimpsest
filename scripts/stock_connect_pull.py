"""Stock Connect telemetry runner — pull HKEX daily-stat prints and publish
readings/stock-connect-latest.json + append the dated history.

Mirrors the china-econ pull pattern: collect -> honesty-guard/abstain ->
write-if-changed -> append history. The history file is append-only and keyed
by DATA date (one JSONL line per trading day); HKEX retention is roughly the
current calendar year, so the git record is the long memory.

Backfill: set STOCK_CONNECT_BACKFILL_DAYS=<n calendar days> to walk further
back than the default window (used once to seed the record; the cron then
only tops up recent days).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from collectors.stock_connect import collect_range

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "stock-connect-latest.json")
HIST = os.path.join(READINGS, "stock-connect-history.jsonl")

WINDOW_DAYS = 14   # normal top-up window per cron run


def _load_history() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if os.path.exists(HIST):
        with open(HIST, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("date"):
                    rows[rec["date"]] = rec
    return rows


def main() -> None:
    now = datetime.now(timezone.utc)
    back = int(os.environ.get("STOCK_CONNECT_BACKFILL_DAYS", "0")) or WINDOW_DAYS

    history = _load_history()
    dates = []
    for i in range(back, -1, -1):
        d = now - timedelta(days=i)
        if d.weekday() >= 5:      # HKEX prints trading days only
            continue
        iso = d.strftime("%Y-%m-%d")
        # On a backfill walk, skip dates the record already carries.
        if back > WINDOW_DAYS and iso in history:
            continue
        dates.append(d.strftime("%Y%m%d"))

    fresh = collect_range(dates)

    # Honesty guard: nothing parsed at all (HKEX down, format change,
    # or an all-holiday window) -> abstain rather than publish hollow.
    if not fresh:
        print("hkex daily stats returned nothing parseable — abstaining")
        return

    new_dates = []
    for date in sorted(fresh):
        row = fresh[date]
        prior = history.get(date)
        # A revisit may complete a partial date — merge, never shrink.
        if prior is None or any(k not in prior for k in row):
            history[date] = {**(prior or {}), **row}
            new_dates.append(date)

    if new_dates:
        with open(HIST, "w", encoding="utf-8") as f:
            for date in sorted(history):
                f.write(json.dumps(history[date], ensure_ascii=False, sort_keys=True) + "\n")

    last_date = max(fresh)
    latest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "HKEX Stock Connect daily statistics (keyless official prints)",
        "asof": last_date,
        "reading": fresh[last_date],
        "history_days": len(history),
        "units": {
            "southbound_net_b": "HKD bn, buy minus sell, SSE+SZSE combined",
            "nb_turnover_b": "CNY bn, total turnover, SSE+SZSE combined",
        },
        "note": (
            "HKEX discontinued the northbound buy/sell split in August 2024; "
            "northbound is turnover-only since. The narrowing is part of the "
            "record — northbound NET flow is never estimated here."
        ),
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"stock-connect: asof {last_date}, {len(new_dates)} new/completed dates, "
          f"{len(history)} days accrued")


if __name__ == "__main__":
    main()
