"""Publish synthetic Greyball calibration. Offline; no network.

Inert unless PALIMPSEST_GREYBALL_ENABLED=1. Abstains when the kill switch is
engaged. Never emits a censorship label.

Usage:  PYTHONPATH=. python -m scripts.greyball_calibration_pull
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.china_observation import iso_z
from core.governance import KillSwitch
from core.greyball_flag import greyball_enabled
from processors.synthetic_calibration import METHOD_VERSION, run_calibration


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "greyball-calibration-latest.json"
HIST = READINGS / "greyball-calibration-history.jsonl"


def main(*, seed: int = 0) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("greyball-calibration: halted by kill switch — abstaining")
        return None
    if not greyball_enabled():
        print("greyball-calibration: inert (set PALIMPSEST_GREYBALL_ENABLED=1) — abstaining")
        return None

    result = run_calibration(seed=seed)
    generated = iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Palimpsest synthetic censorship calibration (offline)",
        "scope": (
            "Eight synthetic processes. If they do not distinguish, Palimpsest "
            "must not emit a censorship label."
        ),
        "all_distinguished": result["all_distinguished"],
        "may_emit_censorship_label": result["may_emit_censorship_label"],
        "censorship_label_emitted": result["censorship_label_emitted"],
        "distinguished": result["distinguished"],
        "predictions": result["predictions"],
        "visibility_labels": result["visibility_labels"],
        "note": result["note"],
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "all_distinguished": out["all_distinguished"],
        }, ensure_ascii=False) + "\n")
    print(
        "greyball-calibration: "
        f"distinguished={out['all_distinguished']} "
        f"censorship_label={out['censorship_label_emitted']}"
    )
    return out


if __name__ == "__main__":
    main()
