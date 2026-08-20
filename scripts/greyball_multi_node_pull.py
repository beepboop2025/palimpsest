"""Multi-node public observation. Outside China only.

Inert unless PALIMPSEST_GREYBALL_ENABLED=1. Without injected observer rows the
runner abstains. Blocked vantages abstain; China-as-sensor rows are rejected.

Usage:  PYTHONPATH=. python -m scripts.greyball_multi_node_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.multi_node_panel import METHOD_VERSION, compare_panel
from core.china_observation import iso_z
from core.governance import KillSwitch
from core.greyball_flag import greyball_enabled


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greyball-multi-node-latest.json"
INBOX = ROOT / "data" / "greyball-observers.jsonl"


def main(*, rows=None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greyball-multi-node: halted by kill switch — abstaining")
        return None
    if not greyball_enabled():
        print("greyball-multi-node: inert (set PALIMPSEST_GREYBALL_ENABLED=1) — abstaining")
        return None
    payload = list(rows or [])
    if not payload and INBOX.exists():
        for line in INBOX.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                payload.append(json.loads(line))
    if not payload:
        print("greyball-multi-node: no observer rows — abstaining")
        return None
    result = compare_panel(payload)
    if result["n_accepted"] == 0 and result["n_abstained"]:
        print("greyball-multi-node: every observer blocked — abstaining")
        return None
    generated = iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Multi-node public observation (outside China)",
        "n_accepted": result["n_accepted"],
        "n_rejected_china_sensor": result["n_rejected_china_sensor"],
        "n_abstained": result["n_abstained"],
        "comparisons": result["comparisons"],
        "rejected": result["rejected"],
        "abstained": result["abstained"],
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        f"greyball-multi-node: accepted={out['n_accepted']} "
        f"rejected_china={out['n_rejected_china_sensor']} abstained={out['n_abstained']}"
    )
    return out


if __name__ == "__main__":
    main()
