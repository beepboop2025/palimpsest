"""GDELT cross-signal runner — triangulate the live DDTI deletion terms against
the open global news record.

Reads the latest published DDTI ranking (readings/ddti-latest.json), asks GDELT
how loudly the world is covering each censored term, and writes
readings/gdelt-latest.json. Two labels fall out per term:

  CONTAINMENT — loud abroad AND censored at home (the state is suppressing a story
                the world is already reporting).
  BLACKOUT    — loud abroad but conspicuously absent at home (suppression by silence).

Standard-library only (urllib + json). Vantage-insensitive: GDELT aggregates
already-published global news, so this runs correctly from anywhere.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.gdelt_cross_signal import enrich_terms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
DDTI_LATEST = os.path.join(READINGS, "ddti-latest.json")
OUT = os.path.join(READINGS, "gdelt-latest.json")
HIST = os.path.join(READINGS, "gdelt-history.jsonl")


def main() -> None:
    if not os.path.exists(DDTI_LATEST):
        print("no readings/ddti-latest.json — run the DDTI signal first; skipping")
        return
    ddti = json.load(open(DDTI_LATEST, encoding="utf-8"))
    ranked = ddti.get("ranked", [])[:25]
    if not ranked:
        print("DDTI ranking empty — skipping")
        return

    terms = [
        {"term": r["term"], "attention": r.get("attention", 1.0), "recent_count": r.get("recent_count", 1)}
        for r in ranked
    ]
    rows = enrich_terms(terms)  # hits GDELT; fails soft per-term (abstains on error)

    # Honesty guard: if GDELT returned no global volume for ANY term (unreachable
    # or rate-limited), abstain rather than publish a hollow all-unknown signal.
    if not any(r.get("global_norm") is not None for r in rows):
        print("GDELT returned no global volume for any term (unreachable / rate-limited) "
              "— abstaining, not publishing a hollow signal")
        return

    now = datetime.now(timezone.utc)
    out = {
        "generated_at": now.isoformat(),
        "source": "GDELT DOC 2.0 API x Palimpsest DDTI terms",
        "scope": "cross-signal: domestic censorship attention vs global coverage volume",
        "ddti_generated_at": ddti.get("generated_at"),
        "n_terms": len(rows),
        "n_with_global_data": sum(1 for r in rows if r.get("global_norm") is not None),
        "n_containment": sum(1 for r in rows if str(r.get("label", "")).lower() == "containment"),
        "n_blackout": sum(1 for r in rows if str(r.get("label", "")).lower() == "blackout"),
        "ranked": rows,
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    top = rows[0] if rows else {}
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "generated_at": out["generated_at"],
            "n_terms": out["n_terms"],
            "n_containment": out["n_containment"],
            "n_blackout": out["n_blackout"],
            "top_term": top.get("term"),
            "top_label": top.get("label"),
        }, ensure_ascii=False) + "\n")

    print(f"=== GDELT cross-signal ({len(rows)} terms; "
          f"{out['n_containment']} containment, {out['n_blackout']} blackout) ===")
    for r in rows[:10]:
        print(f"  {str(r.get('term'))[:28]:<28} {str(r.get('label')):<12} "
              f"cross={float(r.get('cross_score', 0) or 0):.3f} global={r.get('global_norm')}")


if __name__ == "__main__":
    main()
