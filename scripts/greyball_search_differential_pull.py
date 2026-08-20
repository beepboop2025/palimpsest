"""Search-differential scorer. Anomalies, not censorship.

Inert unless PALIMPSEST_GREYBALL_ENABLED=1. Without injected observations the
runner abstains — it does not discover terms by hitting live search.

Usage:  PYTHONPATH=. python -m scripts.greyball_search_differential_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.china_observation import iso_z
from core.governance import KillSwitch
from core.greyball_flag import greyball_enabled
from processors.search_differential import METHOD_VERSION, load_panel, score_differential


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greyball-search-differential-latest.json"


def main(*, observations=None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greyball-search-differential: halted by kill switch — abstaining")
        return None
    if not greyball_enabled():
        print("greyball-search-differential: inert (set PALIMPSEST_GREYBALL_ENABLED=1) — abstaining")
        return None
    rows = list(observations or [])
    if not rows:
        print("greyball-search-differential: no injected observations — abstaining")
        return None
    result = score_differential(rows, panel=load_panel())
    if result.get("status") == "abstained":
        print(f"greyball-search-differential: {result.get('reason')} — abstaining")
        return None
    generated = iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Gazetteer search-result differential (Greyball)",
        "n_observations": result["n_observations"],
        "status": result["status"],
        "visibility_label": result["visibility_label"],
        "censorship_label": result["censorship_label"],
        "anomalies": result["anomalies"],
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"greyball-search-differential: {out['status']} n={out['n_observations']}")
    return out


if __name__ == "__main__":
    main()
