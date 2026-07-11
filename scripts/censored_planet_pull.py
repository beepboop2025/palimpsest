"""Censored Planet signal runner — publish readings/censored-planet-latest.json:
China's interference rate + recent censorship-alert events, measured by a method
independent of OONI. Vantage-insensitive, key-less, stdlib-only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from collectors.censored_planet import cn_events, cn_interference_rate, cn_timeseries

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "censored-planet-latest.json")
HIST = os.path.join(READINGS, "censored-planet-history.jsonl")

WINDOW_DAYS = 70   # CP publishes ~weekly; a wide window guarantees coverage


def main() -> None:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    until = now.strftime("%Y-%m-%d")

    rate = cn_interference_rate(since, until)
    events = cn_events(since, until)
    series = cn_timeseries(since, until)

    # Honesty guard: if the API gave us neither a rate nor events nor a series,
    # abstain rather than publish an empty board.
    if rate is None and not events and not series:
        print("Censored Planet returned no CN data (unreachable / empty) — abstaining")
        return

    # most-recent events first
    events = sorted(events, key=lambda e: (e.get("endDate") or e.get("startDate") or ""),
                    reverse=True)

    out = {
        "generated_at": now.isoformat(),
        "source": "Censored Planet (data.censoredplanet.org) — open GraphQL API",
        "scope": ("independent China censorship measurement — remote DNS/HTTP side-channel "
                  "from 95k+ vantage points (Satellite + Hyperquack), a different method than OONI"),
        "method": "ingests Censored Planet's public aggregated data; probes nothing (vantage-insensitive)",
        "window_days": WINDOW_DAYS,
        "since": since, "until": until,
        "cn_interference_rate_pct": rate,
        "n_events": len(events),
        "events": events[:15],
        "series_points": len(series),
        "series_tail": series[-30:],
        "persistence_note": ("China shows a persistent interference rate but few/no discrete "
                             "'alert events' — CenAlert flags anomalous censorship SPIKES, and "
                             "China's censorship is a continuous baseline, not episodic. Persistent, "
                             "not event-driven, is itself the finding."),
        "note": ("CP's interference rate uses a different denominator/method than OONI's "
                 "anomaly rate — they are not the same number; the value is two independent "
                 "methods both detecting China interference"),
    }
    os.makedirs(READINGS, exist_ok=True)

    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
    changed = any(prev.get(k) != out.get(k) for k in
                  ("cn_interference_rate_pct", "n_events", "events", "series_points"))
    if changed or not prev:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "cn_interference_rate_pct": rate,
                "n_events": len(events),
            }, ensure_ascii=False) + "\n")

    print(f"=== Censored Planet — CN interference {rate}% "
          f"({len(events)} alert events, {len(series)} series pts) ===")
    for e in events[:6]:
        print(f"  {e.get('startDate','?')}..{e.get('endDate','?')}  "
              f"cause={e.get('cause','?')} impact={e.get('impact','?')} peak={e.get('peak','?')}")


if __name__ == "__main__":
    main()
