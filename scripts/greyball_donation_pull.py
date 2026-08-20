"""Volunteer donation ingest. Hashes / transitions / counts only.

Inert unless PALIMPSEST_GREYBALL_ENABLED=1. An empty inbox abstains.

Usage:  PYTHONPATH=. python -m scripts.greyball_donation_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from collectors.greyball_donation import METHOD_VERSION, DonationRejected, ingest_donation
from core.china_observation import iso_z
from core.governance import KillSwitch, RateCeiling
from core.greyball_flag import greyball_enabled


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greyball-donation-latest.json"
INBOX = ROOT / "data" / "greyball-donations.jsonl"


def main(*, payloads=None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greyball-donation: halted by kill switch — abstaining")
        return None
    if not greyball_enabled():
        print("greyball-donation: inert (set PALIMPSEST_GREYBALL_ENABLED=1) — abstaining")
        return None
    rows = list(payloads or [])
    if not rows and INBOX.exists():
        for line in INBOX.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        print("greyball-donation: empty inbox — abstaining")
        return None
    accepted, rejected = [], []
    ceiling = RateCeiling(rate=1.0, capacity=1.0)
    for payload in rows:
        try:
            accepted.append(
                ingest_donation(payload, kill_switch=kill, rate_ceiling=ceiling)
            )
        except DonationRejected as exc:
            rejected.append({"reason": str(exc)})
    generated = iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Volunteer public-field donation (hashes/transitions/counts)",
        "n_accepted": len(accepted),
        "n_rejected": len(rejected),
        "accepted": accepted,
        "rejected": rejected,
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"greyball-donation: accepted={out['n_accepted']} rejected={out['n_rejected']}")
    return out


if __name__ == "__main__":
    main()
