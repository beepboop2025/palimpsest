"""Outside-China observer registry. Refuse China-as-sensor and residential proxy.

Inert unless PALIMPSEST_GREYBALL_ENABLED=1. Without injected observer rows the
runner abstains. Blocked vantages abstain. AS24940 rows collapse to one backer.

Usage:  PYTHONPATH=. python -m scripts.greyball_observers_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.greyball_observers import METHOD_VERSION, compare_panel
from core.china_observation import iso_z
from core.governance import KillSwitch, RateCeiling
from core.greyball_flag import greyball_enabled


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greyball-observers-latest.json"
INBOX = ROOT / "data" / "greyball-observers.jsonl"


def main(*, rows=None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greyball-observers: halted by kill switch — abstaining")
        return None
    if not greyball_enabled():
        print("greyball-observers: inert (set PALIMPSEST_GREYBALL_ENABLED=1) — abstaining")
        return None
    payload = list(rows or [])
    if not payload and INBOX.exists():
        for line in INBOX.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                payload.append(json.loads(line))
    if not payload:
        print("greyball-observers: no observer rows — abstaining")
        return None
    result = compare_panel(
        payload,
        kill_switch=kill,
        rate_ceiling=RateCeiling(rate=1.0, capacity=8.0),
    )
    if result["n_accepted"] == 0 and result["n_abstained"]:
        print("greyball-observers: every observer blocked — abstaining")
        return None
    generated = iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Outside-China observer registry (Greyball)",
        "n_accepted": result["n_accepted"],
        "n_rejected_china_sensor": result["n_rejected_china_sensor"],
        "n_abstained": result["n_abstained"],
        "n_independent_backers": result["n_independent_backers"],
        "comparisons": result["comparisons"],
        "rejected": result["rejected"],
        "abstained": result["abstained"],
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        f"greyball-observers: accepted={out['n_accepted']} "
        f"backers={out['n_independent_backers']} "
        f"rejected_china={out['n_rejected_china_sensor']} abstained={out['n_abstained']}"
    )
    return out


if __name__ == "__main__":
    main()
