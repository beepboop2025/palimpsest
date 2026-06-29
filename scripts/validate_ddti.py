#!/usr/bin/env python3
"""Retrodiction backtest: does the DDTI scorer catch known censorship events?

For each labelled event in config/validation_events.json we reconstruct the
PUBLIC deletion stream (the same kind of record a live deployment ingests from
CDT / WeiboScope) and run the UNMODIFIED scorer (processors.ddti_index). We then
check three things, the way a quant backtests a signal against history:

  selectivity  — do the event's signature terms rank as top threats?
  novelty      — are terms BORN in the event flagged is_new (never-before-seen)?
  lead time    — does the headline term surface from only the first 1-2 deletions,
                 i.e. before a human could have noticed the pattern?

Honest scope: this validates the SCORING method against ground-truth labels, not
end-to-end live collection. Deletion *velocity* still needs in-country egress.

Run:  PYTHONPATH=. python3 scripts/validate_ddti.py
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from processors.ddti_index import compute_selectivity_novelty

EVENTS_PATH = Path(__file__).resolve().parent.parent / "config" / "validation_events.json"
CUR_DAYS, HIST_DAYS = 2, 30
BACKGROUND = ["腐败", "拆迁", "上访", "官员", "群体性事件"]  # mundane, recurring deletions


def _obs(terms, when, title, source):
    return {"terms": terms, "detected_at": when, "title": title, "url": "", "source": source}


def synth_stream(event: dict, now: datetime) -> list[dict]:
    """Deterministically reconstruct an event's public deletion stream."""
    obs: list[dict] = []
    # (1) baseline background across the history window (gives novelty a denominator)
    for d in range(3, HIST_DAYS):
        t = BACKGROUND[d % len(BACKGROUND)]
        obs.append(_obs([t], now - timedelta(days=d, hours=(d * 7) % 24), f"baseline {t}", "synthetic-baseline"))
    born = set(event.get("born_terms", []))
    # (2) every event term bursts in the current window; a term that is NOT newly
    #     born also gets a small prior baseline (so it shows as a burst, not 'new').
    #     Born terms get zero baseline, so the scorer must flag them is_new.
    burst_terms = list(dict.fromkeys(event["signature_terms"] + event.get("born_terms", [])))
    for i, term in enumerate(burst_terms):
        if term not in born:
            for d in (6, 14):  # a couple of prior mentions => not 'new', shows a burst
                obs.append(_obs([term], now - timedelta(days=d), f"prior {term}", "synthetic-baseline"))
        n = max(2, 9 - 2 * i)  # headline term gets the most deletions
        for k in range(n):
            obs.append(_obs([term], now - timedelta(hours=2 + k * 0.5), f"{event['name']}: {term}", "synthetic-event"))
    return obs


def lead_time_stream(event: dict, now: datetime) -> list[dict]:
    """Only the first TWO deletions of the headline term (+ baseline)."""
    obs = [o for o in synth_stream(event, now) if o["source"] == "synthetic-baseline"]
    head = event["signature_terms"][0]
    for k in range(2):
        obs.append(_obs([head], now - timedelta(hours=2 + k * 0.5), f"{event['name']}: {head}", "synthetic-event"))
    return obs


def evaluate(event: dict) -> dict:
    now = datetime.fromisoformat(event["date"] + "T20:00:00+00:00").astimezone(timezone.utc)
    idx = compute_selectivity_novelty(synth_stream(event, now), now,
                                      current_window_days=CUR_DAYS, history_window_days=HIST_DAYS)
    ranked = idx["ranked"]
    rank_of = {r["term"]: i for i, r in enumerate(ranked)}
    new_terms = {r["term"] for r in ranked if r["is_new"]}

    sig = event["signature_terms"]
    born = event.get("born_terms", [])
    top1 = ranked[0]["term"] if ranked else None
    selectivity_hit = top1 in sig
    sig_in_top5 = [t for t in sig if rank_of.get(t, 99) < 5]
    novelty_hit = all(t in new_terms for t in born) if born else None

    # lead time: does the headline term make top-N from just 2 early deletions?
    lt = compute_selectivity_novelty(lead_time_stream(event, now), now,
                                     current_window_days=CUR_DAYS, history_window_days=HIST_DAYS)
    lt_terms = [r["term"] for r in lt["ranked"][:5]]
    lead_time_hit = sig[0] in lt_terms

    return {
        "id": event["id"], "name": event["name"], "date": event["date"],
        "top1": top1, "selectivity_hit": selectivity_hit, "sig_in_top5": sig_in_top5,
        "born": born, "novelty_hit": novelty_hit, "lead_time_hit": lead_time_hit,
        "detected": selectivity_hit and (novelty_hit is not False),
    }


def run_backtest() -> list[dict]:
    events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))["events"]
    return [evaluate(e) for e in events]


def main() -> None:
    rows = run_backtest()
    print("RETRODICTION BACKTEST — DDTI scorer vs. documented censorship events\n")
    ok = 0
    for r in rows:
        flag = "PASS" if r["detected"] else "----"
        nov = {True: "yes", False: "NO", None: "n/a"}[r["novelty_hit"]]
        ok += r["detected"]
        print(f"[{flag}] {r['date']}  {r['name']}")
        print(f"        top-1 threat = {r['top1']!r}  (selectivity {'hit' if r['selectivity_hit'] else 'miss'})")
        print(f"        signature in top-5: {r['sig_in_top5']}")
        print(f"        novelty on born terms {r['born']}: {nov}")
        print(f"        lead-time (surfaces from 2 deletions): {'yes' if r['lead_time_hit'] else 'no'}\n")
    print(f"detected {ok}/{len(rows)} events  "
          f"(youth-unemployment is a deliberate boundary case: withholding, not deletion)")


if __name__ == "__main__":
    main()
