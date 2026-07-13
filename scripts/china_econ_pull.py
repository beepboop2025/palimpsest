"""China econ telemetry runner — pull CFETS official money-market benchmarks
and publish readings/china-econ-latest.json + append the dated history.

Mirrors the OONI/DDTI pull pattern: collect -> honesty-guard/abstain ->
write-if-changed -> append history. The history file is append-only and keyed
by DATA date (one JSONL line per trading day), so the git record outgrows the
portal's ~1-month request window run by run.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from collectors.china_econ import collect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "china-econ-latest.json")
HIST = os.path.join(READINGS, "china-econ-history.jsonl")

WINDOW_DAYS = 28   # inside the portal's per-request range limit


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
    start = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    fresh = collect(start, end)

    # Honesty guard: if no benchmark family answered at all (throttled or the
    # portal is down), abstain rather than publish a hollow reading.
    if not fresh:
        print("chinamoney returned nothing for any benchmark family — "
              "abstaining, not publishing")
        return

    history = _load_history()
    new_dates = []
    for date in sorted(fresh):
        row = {"date": date, **fresh[date]}
        prior = history.get(date)
        # A revisit may complete a date a throttled run left partial — merge,
        # never shrink.
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
        "source": "CFETS chinamoney English portal (official published benchmarks, keyless)",
        "asof": last_date,
        "benchmarks": fresh[last_date],
        "families_reporting": sorted({
            fam for row in fresh.values() for fam in (
                ("shibor",) if any(k.startswith("shibor") for k in row) else ()
            )
        } | {
            fam for row in fresh.values() for fam in (
                ("repo_fixing",) if any(k.startswith(("fr0", "fdr")) for k in row) else ()
            )
        } | {
            fam for row in fresh.values() for fam in (
                ("central_parity",) if "usdcny_parity" in row else ()
            )
        }),
        "history_days": len(history),
        "note": (
            "official state-published numbers; the policy read is FDR007 vs the "
            "7-day OMO rate and the daily parity fix, not any single level"
        ),
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"china-econ: asof {last_date}, {len(new_dates)} new/completed dates, "
          f"{len(history)} days accrued, families {latest['families_reporting']}")


if __name__ == "__main__":
    main()
