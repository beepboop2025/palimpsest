#!/usr/bin/env python3
"""The Censorship Fear Index across documented events — a retrodicted time-series.

For each labelled event we compute the Fear Index on the event window and on its own
quiet baseline, so you can see the single number spike when the state moves to bury
something. This is the public-facing distillation of the DDTI: one figure a journalist
or funder reads at a glance.

Run:  PYTHONPATH=. python3 scripts/fear_index_demo.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from processors.ddti_index import compute_selectivity_novelty
from processors.fear_index import compute_fear_index
from scripts.validate_ddti import CUR_DAYS, HIST_DAYS, EVENTS_PATH, synth_stream


def fear_for(event: dict) -> tuple[dict, dict]:
    now = datetime.fromisoformat(event["date"] + "T20:00:00+00:00").astimezone(timezone.utc)
    full = synth_stream(event, now)
    base = [o for o in full if o["source"] == "synthetic-baseline"]
    di_e = compute_selectivity_novelty(full, now, current_window_days=CUR_DAYS, history_window_days=HIST_DAYS)
    di_b = compute_selectivity_novelty(base, now, current_window_days=CUR_DAYS, history_window_days=HIST_DAYS)
    return compute_fear_index(di_e, now=now), compute_fear_index(di_b, now=now)


def _bar(v: float, width: int = 44) -> str:
    n = int(round(v / 100 * width))
    return "█" * n + "·" * (width - n)


def main() -> None:
    events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))["events"]
    print("CENSORSHIP FEAR INDEX — documented events vs. their baselines\n")
    print(f"  {'scale':>10}  0{'·' * 42}100\n")
    for e in events:
        fe, fb = fear_for(e)
        delta = round(fe["index"] - fb["index"], 1)
        print(f"  {e['date']}  {e['name']}")
        print(f"      baseline {fb['index']:>5} [{fb['band']:<8}] {_bar(fb['index'])}")
        print(f"      EVENT    {fe['index']:>5} [{fe['band']:<8}] {_bar(fe['index'])}  (+{delta})")
        print(f"      → {fe['interpretation']}\n")
    print("  Selectivity & novelty drive this today; a federated/seam vantage adds the\n"
          "  velocity component (currently suppressed — reported fail-loud, never faked).")


if __name__ == "__main__":
    main()
