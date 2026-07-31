"""Vantage-fusion runner — fuse the three China network-censorship vantages
into one calibrated anomaly rate with a corroboration measure, and publish
readings/vantage-fusion-latest.json.

Pure recomputation over the committed vantage readings (no network), so anyone
can reproduce the fused number offline. The reading is rewritten every cycle and
carries last_changed_at; history appends only when the fused index or confidence
tier changes materially, not every cycle.
"""
from __future__ import annotations

import json
import os

from processors.vantage_fusion import fuse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "vantage-fusion-latest.json")
HIST = os.path.join(READINGS, "vantage-fusion-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see. This driver rewrites
# the reading unconditionally, so it cannot hide a method change behind an
# unchanged number. Carried as provenance: a reader diffing two history rows
# needs to know whether the method moved underneath them.
METHOD_VERSION = 1



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
    reading["method_version"] = METHOD_VERSION
    if not reading.get("ok"):
        print("fusion abstained:", reading.get("reason"))
        return

    previous = _load("vantage-fusion-latest.json")

    moved = (
        previous.get("confidence") != reading["confidence"]
        or abs((previous.get("fused_index") or 0) - reading["fused_index"]) >= 2.0
    )

    # "When did we last look" and "when did the fused answer last move" are
    # different questions, and this file only ever answered the first. The
    # reading has always been rewritten every cycle, so generated_at is honest
    # about the observation time — but with nothing carrying the movement, a
    # freshly stamped file reads as though the fused index had just moved when
    # in fact it has held its ground for weeks. A refreshed timestamp on a still
    # number is news the measurement did not report. So generated_at keeps this
    # round's observation time and last_changed_at carries the movement, gated
    # on the same materiality test the history file uses so the two can never
    # tell different stories. Files published before this field existed fall
    # back to their own generated_at, which is the honest reading of when they
    # last moved. Nothing about the abstain paths changes: a round that never
    # got a fused number still returns above without writing anything.
    reading["last_changed_at"] = (
        reading["generated_at"] if (moved or not previous)
        else (previous.get("last_changed_at") or previous.get("generated_at")))

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(reading, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

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
        print(f"no material change since {reading['last_changed_at']} — "
              f"republished with this round's observation time, history untouched")
    print(reading["verdict"])


if __name__ == "__main__":
    main()
