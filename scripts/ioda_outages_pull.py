"""IODA outage runner — pull the CN outage window and publish the signal.

History is keyed by the RUN date (IODA events are point-in-time detections;
the trailing 7-day window smooths late detections, and the per-day history row
records the count of events that STARTED that day, so the series is stable
under window overlap).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from collectors.ioda_outages import collect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "ioda-outages-latest.json")
HIST = os.path.join(READINGS, "ioda-outages-history.jsonl")

WINDOW_DAYS = 7


def main() -> None:
    now = datetime.now(timezone.utc)
    until = int(now.timestamp())
    frm = int((now - timedelta(days=WINDOW_DAYS)).timestamp())

    got = collect(frm, until)
    if got is None:
        print("ioda returned nothing parseable — abstaining")
        return

    events = got.get("events") or []
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    ev_yesterday = [
        e for e in events
        if datetime.fromtimestamp(e["start"], tz=timezone.utc)
        .strftime("%Y-%m-%d") == yesterday]

    latest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": ("IODA (Georgia Tech Internet Intelligence) v2 API — outage "
                   "events for country CN from BGP, active probing "
                   "(ping-slash24) and darknet (merit-nt) instruments"),
        "window_days": WINDOW_DAYS,
        "summary": got.get("summary"),
        "events": events,
        "instruments_firing": got.get("instruments_firing", 0),
        "method_note": (
            "Shutdown-scale connectivity events, detected from outside China "
            "by three independent global instruments. Each event names the "
            "instrument that saw it; a single-instrument event is a candidate, "
            "multi-instrument corroboration is the strong read. The conformal "
            "signal is the per-day count of event STARTS."
        ),
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    hist_row = {
        "date": today,
        "generated_at": latest["generated_at"],
        "events_started_yesterday": len(ev_yesterday),
        "window_event_cnt": (got.get("summary") or {}).get("event_cnt", 0),
        "instruments_firing": got.get("instruments_firing", 0),
    }
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(hist_row, ensure_ascii=False, sort_keys=True) + "\n")

    print(f"ioda-outages: {len(events)} events in {WINDOW_DAYS}d window, "
          f"{len(ev_yesterday)} started {yesterday}, "
          f"{got.get('instruments_firing', 0)} instruments firing")


if __name__ == "__main__":
    main()
