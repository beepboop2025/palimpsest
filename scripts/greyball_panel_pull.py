"""Official-first-seen + Telegram preview panel. No followers or personal accounts.

Inert unless PALIMPSEST_GREYBALL_ENABLED=1. Without injected public rows the
runner abstains.

Usage:  PYTHONPATH=. python -m scripts.greyball_panel_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.greyball_panel import METHOD_VERSION, GreyballPanelError, monitor_official_and_telegram
from core.china_observation import iso_z
from core.governance import KillSwitch, RateCeiling
from core.greyball_flag import greyball_enabled


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greyball-panel-latest.json"


def main(*, rows=None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greyball-panel: halted by kill switch — abstaining")
        return None
    if not greyball_enabled():
        print("greyball-panel: inert (set PALIMPSEST_GREYBALL_ENABLED=1) — abstaining")
        return None
    payload = list(rows or [])
    if not payload:
        print("greyball-panel: no official/Telegram rows — abstaining")
        return None
    try:
        result = monitor_official_and_telegram(
            payload,
            kill_switch=kill,
            rate_ceiling=RateCeiling(rate=1.0, capacity=1.0),
        )
    except GreyballPanelError as exc:
        print(f"greyball-panel: refused ({exc}) — abstaining")
        return None
    generated = iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Official-first-seen + Telegram previews (Greyball)",
        "n_accounts": result["n_accounts"],
        "collects_followers": False,
        "collects_personal_accounts": False,
        "accounts": result["accounts"],
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"greyball-panel: accounts={out['n_accounts']}")
    return out


if __name__ == "__main__":
    main()
