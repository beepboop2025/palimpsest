"""Forecast-ledger runner — publish readings/forecast-ledger-latest.json.

The observatory's own track record: every signal forecast one step ahead from
only its past, then scored against what actually arrived, with the misses named.
Pure recomputation from committed histories — no network — so anyone can rerun
this over the same files and reproduce every number, including the bad ones.
"""
from __future__ import annotations

import json
import os

from processors.forecast_ledger import build_reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "forecast-ledger-latest.json")
HIST = os.path.join(READINGS, "forecast-ledger-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see. This driver rewrites
# the reading unconditionally, so it cannot hide a method change behind an
# unchanged number. Carried as provenance: a reader diffing two history rows
# needs to know whether the method moved underneath them.
METHOD_VERSION = 1



def main() -> None:
    reading = build_reading(READINGS)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(reading, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    # The ledger is a cumulative record, so every run appends: the point is to be
    # able to see the track record itself move over time.
    entry = {
        "generated_at": reading["generated_at"],
        "method_version": METHOD_VERSION,
        "n_signals_scored": reading["n_signals_scored"],
        "n_forecasts": reading["n_forecasts"],
        "pooled_empirical_coverage": reading["pooled_empirical_coverage"],
        "nominal_coverage": reading["nominal_coverage"],
        "n_beating_baseline": reading["n_beating_baseline"],
        "per_signal": {
            k: {"coverage": v["empirical_coverage"], "wis": v["wis"],
                "beats_baseline": v["beats_baseline"], "n": v["n_forecasts"]}
            for k, v in reading["signals"].items()
        },
    }
    with open(HIST, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(reading["headline"])


if __name__ == "__main__":
    main()
