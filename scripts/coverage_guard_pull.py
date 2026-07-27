"""Coverage-guard runner — publish readings/coverage-guard-latest.json.

For every signal whose sample size is recorded, asks whether its latest movement
survives conditioning on that sample size. A censorship number resting on a
shrinking measurement base can move for reasons that have nothing to do with the
censor, and publishing the second when the first happened is the most likely way
this observatory would say something false.

Pure recomputation from committed histories — no network, fully reproducible
offline. History appends on CHANGE of the per-signal verdict set.
"""
from __future__ import annotations

import json
import os

from processors.coverage_guard import build_reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "coverage-guard-latest.json")
HIST = os.path.join(READINGS, "coverage-guard-history.jsonl")


def _verdicts(reading: dict) -> dict:
    return {k: v.get("verdict") for k, v in reading.get("signals", {}).items()}


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

    if previous is None or _verdicts(previous) != _verdicts(reading):
        entry = {"generated_at": reading["generated_at"],
                 "verdicts": _verdicts(reading),
                 "confounded": reading["confounded"]}
        with open(HIST, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"verdict change logged: {entry['verdicts']}")
    else:
        print("no verdict change")
    print(reading["headline"])


if __name__ == "__main__":
    main()
