"""Frozen SERP vocabulary runner. Anomalies, not censorship.

Inert unless PALIMPSEST_GREYBALL_ENABLED=1. Without injected observations the
runner abstains — it does not mutate terms to hunt blocks.

Usage:  PYTHONPATH=. python -m scripts.greyball_serp_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.greyball_serp import METHOD_VERSION, load_panel, score_differential
from core.china_observation import iso_z
from core.governance import KillSwitch, RateCeiling
from core.greyball_flag import greyball_enabled


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greyball-serp-latest.json"


def main(*, observations=None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greyball-serp: halted by kill switch — abstaining")
        return None
    if not greyball_enabled():
        print("greyball-serp: inert (set PALIMPSEST_GREYBALL_ENABLED=1) — abstaining")
        return None
    rows = list(observations or [])
    if not rows:
        print("greyball-serp: no injected observations — abstaining")
        return None
    result = score_differential(
        rows,
        panel=load_panel(),
        kill_switch=kill,
        rate_ceiling=RateCeiling(rate=1.0, capacity=1.0),
    )
    if result.get("status") == "abstained":
        print(f"greyball-serp: {result.get('reason')} — abstaining")
        return None
    generated = iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Frozen SERP vocabulary (Greyball method 6)",
        "n_observations": result["n_observations"],
        "status": result["status"],
        "visibility_label": result["visibility_label"],
        "censorship_label": result["censorship_label"],
        "frozen": True,
        "anomalies": result["anomalies"],
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"greyball-serp: {out['status']} n={out['n_observations']}")
    return out


if __name__ == "__main__":
    main()
