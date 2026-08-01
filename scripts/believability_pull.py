"""Believability runner — the Li Keqiang composite against the headline, from
the state's own releases, published with its uncertainty band.

Monthly. The data month is the PREVIOUS calendar month (June data lands
mid-July; this runs on the 18th, after the NBS 15th-17th window, the PBC
report and the NRA mid-month news). Components whose article does not match
the expected data month are dropped by the collector — a stale month reused
as a fresh one would be exactly the fabrication this project refuses.

The math and the abstention discipline live in processors/believability.py:
the canonical 40/40/20 composite abstains unless EVERY component is present,
the drift claim abstains until MIN_HISTORY months of gap history exist, and
every reading carries the expected band (uncertainty first). The full method
note — origin, critiques, counterpoint, priors ledger — is
docs/BELIEVABILITY.md, and every reading links it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.lkq_telemetry import collect
from processors.believability import LKQ_WEIGHTS, MIN_HISTORY, divergence_reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "believability-latest.json")
HIST = os.path.join(READINGS, "believability-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see. The reading is
# rewritten unconditionally, so a method change can never hide behind an
# unchanged number.
METHOD_VERSION = 1


def _expected_period(now: datetime) -> str:
    y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def _load_history() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if os.path.exists(HIST):
        with open(HIST, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("month"):
                    rows[rec["month"]] = rec
    return rows


def main() -> None:
    now = datetime.now(timezone.utc)
    period = _expected_period(now)

    got = collect(period)
    components = got.get("components", {})
    headline = got.get("headline_ip_yoy")

    # Honesty guard: nothing at all answered for the period — no reading.
    if not components and headline is None:
        print(f"no component or headline for {period} answered — "
              "abstaining, not publishing")
        return

    history = _load_history()
    gap_history = [history[m]["gap"] for m in sorted(history)
                   if m < period and isinstance(history[m].get("gap"), (int, float))]

    reading = divergence_reading(headline, components, gap_history)

    # No run timestamp in the history row: the row is keyed by DATA month and
    # must change only when the answer changes, or every monthly re-run would
    # churn the committed file for nothing (the write-if-changed lesson, in
    # its history-side form). "When did we last look" lives in the reading.
    row = {
        "month": period,
        "components": components,
        "headline_ip_yoy": headline,
        "gap": reading["gap"],
        "label": reading["label"],
    }
    prev = history.get(period)
    downgrade = (prev is not None
                 and isinstance(prev.get("gap"), (int, float))
                 and row["gap"] is None)
    if downgrade:
        # A re-run that hit a transient source failure must not overwrite a
        # complete month with a hollow one: that would delete the month from
        # every future drift baseline. The reading still publishes this
        # round's (degraded) look; the history keeps the better answer.
        print(f"history kept: {period} already has a complete row and this "
              "round collected less")
    elif prev != row:
        history[period] = row
        with open(HIST, "w", encoding="utf-8") as f:
            for m in sorted(history):
                f.write(json.dumps(history[m], ensure_ascii=False, sort_keys=True) + "\n")

    missing = sorted(set(LKQ_WEIGHTS) - set(components))
    latest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method_version": METHOD_VERSION,
        "source": (
            "the state's own releases, keyless: NBS English press releases "
            "(Energy Production; Industrial Production Operation), PBC "
            "monthly financial statistics report (outstanding RMB loan YoY), "
            "NRA monthly rail freight news"
        ),
        "method_note": (
            "the Li Keqiang composite (40% loan growth, 40% electricity, 20% "
            "rail freight — the canonical weights) against the state's own "
            "headline industrial-production YoY for the same month. The "
            "statistic is DRIFT: this month's headline-minus-composite gap "
            "against the gap's own rolling median and MAD band, so a "
            "services-heavy economy's persistent gap reads as baseline and "
            "only a break in the established relationship registers. "
            "Electricity is PRODUCTION (the keyless state series), not the "
            "original's consumption — a stated substitution. A missing "
            "component abstains the composite rather than reweighting it; "
            "fewer than " + str(MIN_HISTORY) + " months of history publishes "
            "the gap but claims no drift (warming_up). Full method, origin, "
            "critiques and counterpoint: docs/BELIEVABILITY.md. A reading "
            "outside the band is evidence the relationship moved; it does "
            "not, by itself, establish fabrication"
        ),
        "asof": period,
        "n_components_present": len(components),
        "n_components_required": len(LKQ_WEIGHTS),
        "components_missing": missing,
        "components": components,
        "telemetry": got.get("telemetry", {}),
        "flags": got.get("flags", []),
        **({"loans_basis": got["loans_basis"]} if got.get("loans_basis") else {}),
        **{k: reading[k] for k in
           ("lkq_composite", "headline_yoy", "gap", "expected_gap", "band_low",
            "band_high", "drift", "outside_band", "label", "n_history")},
        "note": (
            "physical telemetry is hard to fake because someone pays for the "
            "electricity; this read compares the state to the state"
        ),
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"believability: {period} composite {reading['lkq_composite']} vs "
          f"headline {headline} -> gap {reading['gap']} ({reading['label']})"
          + (f", missing: {', '.join(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
