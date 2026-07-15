"""Conformal event-flags runner — score every observatory signal against its
own history with anytime-valid conformal test martingales and publish
readings/event-flags-latest.json.

Pure recomputation: reads only the signal history JSONLs already in the repo
(no network), so anyone can verify every flag offline by rerunning this script
over the same files. History appends only on STATE CHANGE — the history file
is a log of transitions (calm->watch->alarm->...), not a heartbeat.
"""
from __future__ import annotations

import json
import os

from processors.conformal_events import build_reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "event-flags-latest.json")
HIST = os.path.join(READINGS, "event-flags-history.jsonl")


def _states(reading: dict) -> dict:
    return {k: v.get("state") for k, v in reading.get("signals", {}).items()}


def main() -> None:
    reading = build_reading(READINGS)

    previous = None
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as fh:
                previous = json.load(fh)
        except (OSError, json.JSONDecodeError):
            previous = None

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(reading, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    if previous is None or _states(previous) != _states(reading):
        entry = {
            "generated_at": reading["generated_at"],
            "states": _states(reading),
            "active": reading["active"],
        }
        with open(HIST, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"state change logged: {entry['states']}")
    else:
        print("no state change")
    print(reading["headline"])


if __name__ == "__main__":
    main()
