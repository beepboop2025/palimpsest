"""Cross-layer runner — publish readings/cross-layer-latest.json.

Does one layer of the censorship apparatus move before another? Every incumbent
observatory sits inside a single layer, so the question has no owner despite all
the inputs being free. Pure recomputation from committed histories, no network.
"""

from __future__ import annotations

import json
import os

from processors.cross_layer import build_reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "cross-layer-latest.json")
HIST = os.path.join(READINGS, "cross-layer-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see, even if the numbers
# do not move. This driver rewrites the reading unconditionally, so it cannot
# hide a method change; the projection guard below controls only whether a
# HISTORY row is appended. Carried as provenance for anyone diffing two rows.
METHOD_VERSION = 1


def _state(reading: dict) -> dict:
    return {
        "n_pairs_tested": reading.get("n_pairs_tested"),
        "confirmed": sorted(
            "+".join(p["pair"])
            for p in reading.get("pairs", [])
            if p.get("survives_multiplicity")
        ),
    }


def main(*, now=None) -> None:
    reading = build_reading(READINGS, now=now)
    reading["method_version"] = METHOD_VERSION

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
        entry = {"generated_at": reading["generated_at"], **_state(reading)}
        with open(HIST, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"cross-layer state change logged: {_state(reading)}")
    else:
        print("no cross-layer state change")
    print(reading["headline"])


if __name__ == "__main__":
    main()
