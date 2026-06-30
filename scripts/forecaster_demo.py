#!/usr/bin/env python3
"""The censorship forecaster — a worked Called Shot.

Given a representative DDTI state, predict (a) which terms will intensify and
(b) which evasion classes will spawn new euphemisms, then assemble the falsifiable
record you would publish + hash-chain.

Run:  PYTHONPATH=. python3 scripts/forecaster_demo.py
"""
from __future__ import annotations

from datetime import datetime, timezone

from processors.forecaster import called_shot

# bare gazetteer terms (as extract_terms would produce), so phylogeny roots match
DDTI = {"scope": "demo", "ranked": [
    {"term": "四通桥", "domain": "POLITICS", "threat": 9.0, "attention": 3.0, "novelty": 1.0, "is_new": True,  "burst_ratio": None},
    {"term": "青年失业率", "domain": "ECONOMY", "threat": 3.4, "attention": 2.2, "novelty": 0.55, "is_new": False, "burst_ratio": 3.4},
    {"term": "白纸", "domain": "POLITICS", "threat": 4.2, "attention": 2.0, "novelty": 0.80, "is_new": False, "burst_ratio": 5.0},
    {"term": "彭帅", "domain": "SOCIETY", "threat": 3.8, "attention": 1.8, "novelty": 1.0, "is_new": True,  "burst_ratio": None},
    {"term": "六四", "domain": "POLITICS", "threat": 2.0, "attention": 1.9, "novelty": 0.10, "is_new": False, "burst_ratio": 1.1},
]}


def main() -> None:
    now = datetime(2026, 6, 30, tzinfo=timezone.utc)
    shot = called_shot(DDTI, region="cn", now=now, horizon_days=7)
    print(f"CALLED SHOT · {shot['generated_at'][:10]} · region {shot['region']} · horizon {shot['horizon_days']}d\n")

    print("  WILL INTENSIFY (escalation forecast):")
    for w in shot["watch_terms"]:
        print(f"    {w['escalation']:>5}  {w['term']:<12} {w['domain']:<10} — {w['rationale']}")

    print("\n  NEW EUPHEMISMS LIKELY (mutation forecast from the phylogeny):")
    for m in shot["watch_mutations"]:
        cand = "; ".join(f"{k}:{v}" for k, v in m["mechanical_candidates"].items()) or "—"
        print(f"    root {m['root']}  observed[{', '.join(m['observed_mechanisms']) or '—'}]"
              f"  → predict[{', '.join(m['predicted_next'])}]  ({m['confidence']})")
        print(f"        derivable now: {cand}")

    print(f"\n  Falsifiable by: {shot['falsifiable_by']}")
    print("  → publish this record + record its hash in core/governance.py; a confirmed call is the proof.")


if __name__ == "__main__":
    main()
