"""Board-alarm runner — publish readings/board-alarm-latest.json.

Answers the question a reader actually asks of the board ("is anything
happening?") with the multiplicity paid for: e-BH selection across all monitored
signals, per-layer and board-wide merged e-values, and the layer-coincidence
count that turns the observatory's cross-layer thesis into a statistic.

Pure recomputation from the signal history JSONLs already committed — no
network, so every number here is reproducible offline by rerunning this script
over the same files. History appends on CHANGE of the board's headline state,
not as a heartbeat, matching the other event-flag logs in this directory.
"""
from __future__ import annotations

import json
import os

from processors.board_alarm import build_reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "board-alarm-latest.json")
HIST = os.path.join(READINGS, "board-alarm-history.jsonl")


def _state(reading: dict) -> dict:
    """The fields whose change is worth a history line."""
    return {
        "elevated_layers": reading.get("elevated_layers"),
        "selected": (reading.get("fdr_selection") or {}).get("selected"),
        "coincidence": reading.get("layer_coincidence"),
    }


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

    if previous is None or _state(previous) != _state(reading):
        entry = {"generated_at": reading["generated_at"],
                 "board_e_value": reading["board_e_value"], **_state(reading)}
        with open(HIST, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"board state change logged: {_state(reading)}")
    else:
        print("no board state change")
    print(reading["headline"])


if __name__ == "__main__":
    main()
