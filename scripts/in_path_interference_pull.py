"""In-Path Interference runner — pull OONI's CN aggregation for the middlebox,
transport and execution test families and publish
readings/in-path-interference-latest.json.

Deliberately separate from the ooni-gfw signal: that one's index only means
something if its test set stays fixed, so these tests get their own reading
rather than silently redefining a published number.

Vantage-insensitive, keyless, standard-library only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from collectors.in_path_interference import (control_state, execution_blackouts,
                                             family_index, observe_all)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "in-path-interference-latest.json")
HIST = os.path.join(READINGS, "in-path-interference-history.jsonl")

# CN measurement volume is uneven day to day; 90 days keeps the thinner tests
# (torsf, echcheck) above the floor without smearing a real change beyond
# recognition.
WINDOW_DAYS = 90


def _middlebox_reading(idx: float | None) -> str:
    if idx is None:
        return "no middlebox test cleared the measurement floor"
    if idx >= 5:
        return (f"{idx:.1f}% of completed probes saw their HTTP rewritten in transit — "
                "widespread in-path tampering")
    if idx >= 1:
        return (f"{idx:.1f}% of completed probes saw their HTTP rewritten in transit — "
                "in-path devices present and active")
    if idx > 0:
        return f"{idx:.1f}% — occasional in-path rewriting"
    return "no completed probe saw its HTTP rewritten in transit"


def _transport_reading(idx: float | None) -> str:
    if idx is None:
        return "no transport test cleared the measurement floor"
    if idx >= 75:
        return f"{idx:.1f}% anomalous — real-time and circumvention transports largely unusable"
    if idx >= 40:
        return f"{idx:.1f}% anomalous — transports substantially degraded"
    if idx >= 10:
        return f"{idx:.1f}% anomalous — transports partly degraded"
    return f"{idx:.1f}% anomalous — transports largely working"


def main() -> None:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    until = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    observations = observe_all(since, until)
    control = control_state(observations)

    if control["state"] == "DEGRADED":
        print(f"in-path-interference: DEGRADED — {control['why']} — abstaining")
        return

    middlebox = family_index(observations, "MIDDLEBOX")
    transport = family_index(observations, "TRANSPORT")
    blackouts = execution_blackouts(observations)

    out = {
        "generated_at": now.isoformat(),
        "source": "OONI aggregation API (api.ooni.io), probe_cc=CN",
        "window_days": WINDOW_DAYS,
        "scope": ("in-path tampering, real-time/circumvention transport health, and "
                  "measurement methods that cannot execute inside China"),
        "method": ("rates are computed over COMPLETED tests only "
                   "(measurement_count - failure_count). OONI's anomaly counter ranges "
                   "only over completed runs, so a test that never completes has no "
                   "anomaly rate — it has an execution-failure rate, reported separately"),
        "control": control,
        "middlebox_index": middlebox,
        "middlebox_reading": _middlebox_reading(middlebox),
        "transport_index": transport,
        "transport_reading": _transport_reading(transport),
        "execution_blackouts": blackouts,
        "execution_reading": (
            f"{len(blackouts)} measurement method(s) never completed a single run in "
            f"China this window: {', '.join(b['test'] for b in blackouts)}"
            if blackouts else "every method completed at least some runs"),
        "tests": observations,
    }

    prev = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, json.JSONDecodeError):
            prev = {}

    changed = (prev.get("middlebox_index") != middlebox
               or prev.get("transport_index") != transport
               or {b["test"] for b in prev.get("execution_blackouts", [])}
               != {b["test"] for b in blackouts})

    os.makedirs(READINGS, exist_ok=True)
    if changed or not prev:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "middlebox_index": middlebox,
                "transport_index": transport,
                "n_execution_blackouts": len(blackouts),
            }, ensure_ascii=False) + "\n")
        print(f"in-path-interference: middlebox={middlebox} transport={transport} "
              f"blackouts={[b['test'] for b in blackouts]}")
    else:
        print(f"in-path-interference: unchanged (middlebox={middlebox}, "
              f"transport={transport}) — not rewriting")


if __name__ == "__main__":
    main()
