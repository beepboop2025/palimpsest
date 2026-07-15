"""Vantage-fusion runner — fuse the three China network-censorship vantages
into one calibrated anomaly rate with a corroboration measure, and publish
readings/vantage-fusion-latest.json.

Pure recomputation over the committed vantage readings (no network), so anyone
can reproduce the fused number offline. History appends only when the fused
index or confidence tier changes materially, not every cycle.
"""
from __future__ import annotations

import json
import os

from processors.vantage_fusion import fuse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "vantage-fusion-latest.json")
HIST = os.path.join(READINGS, "vantage-fusion-history.jsonl")


def _load(name: str) -> dict:
    path = os.path.join(READINGS, name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    reading = fuse({
        "ooni": _load("ooni-gfw-latest.json"),
        "censored_planet": _load("censored-planet-latest.json"),
        "net4people": _load("net4people-latest.json"),
    })
    if not reading.get("ok"):
        print("fusion abstained:", reading.get("reason"))
        return

    previous = _load("vantage-fusion-latest.json")
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(reading, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    moved = (
        previous.get("confidence") != reading["confidence"]
        or abs((previous.get("fused_index") or 0) - reading["fused_index"]) >= 2.0
    )
    if moved:
        entry = {"generated_at": reading["generated_at"],
                 "fused_index": reading["fused_index"],
                 "confidence": reading["confidence"],
                 "agreement": reading["agreement"],
                 "vantages": reading["vantages"]}
        with open(HIST, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print("logged:", entry["fused_index"], entry["confidence"])
    else:
        print("no material change")
    print(reading["verdict"])


if __name__ == "__main__":
    main()
