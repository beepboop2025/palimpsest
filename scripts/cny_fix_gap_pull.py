"""CNY fix-gap runner — publish readings/cny-fix-gap-latest.json + the dated
history of spot-vs-fix divergence.

Backdrop family (china-econ, stock-connect): published as a reading, kept off
the alarm board. The honest rule this driver adds to the collector's legs: a
gap is computed ONLY for a date where both legs exist. ECB prints CNY on
Chinese holidays when there is no fix; publishing a "gap" against a fix that
was never set that day would manufacture divergence out of a calendar.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.cny_fix_gap import fetch_spot, read_parity

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "cny-fix-gap-latest.json")
HIST = os.path.join(READINGS, "cny-fix-gap-history.jsonl")
CHINA_ECON_HIST = os.path.join(READINGS, "china-econ-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see. The reading is
# rewritten unconditionally, so a method change can never hide behind an
# unchanged number.
METHOD_VERSION = 1


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
                if rec.get("date"):
                    rows[rec["date"]] = rec
    return rows


def main() -> None:
    now = datetime.now(timezone.utc)

    spot = fetch_spot()
    if not spot:
        print("no spot leg (ECB unreachable or unparseable) — abstaining, not publishing")
        return

    parity = read_parity(CHINA_ECON_HIST)
    fix = parity.get(spot["date"])
    if fix is None:
        # ECB printed a CNY rate but no fix exists for that date (Chinese
        # holiday, or the china-econ round has not landed yet). No fix, no gap.
        print(f"no parity for {spot['date']} — no fix was set, so no gap exists; "
              "abstaining, not publishing")
        return

    gap = round(spot["usdcny"] - fix, 6)
    gap_pct = round(100.0 * gap / fix, 4)

    row = {
        "date": spot["date"],
        "usdcny_spot_ecb": spot["usdcny"],
        "usdcny_parity": fix,
        "gap": gap,
        "gap_pct": gap_pct,
    }
    if "cross_check_diff" in spot:
        row["cross_check_diff"] = spot["cross_check_diff"]

    history = _load_history()
    if history.get(row["date"]) != row:
        history[row["date"]] = row
        with open(HIST, "w", encoding="utf-8") as f:
            for d in sorted(history):
                f.write(json.dumps(history[d], ensure_ascii=False, sort_keys=True) + "\n")

    latest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method_version": METHOD_VERSION,
        "source": (
            "PBOC USD/CNY central parity (via this repo's china-econ history, "
            "CFETS-published) vs ECB euro foreign exchange reference rates "
            "(USD/CNY derived as EURCNY/EURUSD; source: ECB), cross-checked "
            "against Bank of Canada Valet noon rates (source: Bank of Canada)"
        ),
        "method_note": (
            "two prices for one currency: the state's 09:15 Beijing fix vs an "
            "independent same-day market reference (ECB concertation, 14:15 "
            "CET = 20:15 Beijing). gap_pct > 0 means the market prices the "
            "yuan weaker than the fix; the band is ±2%, so gap_pct is read "
            "against that scale. Computed only on days a fix exists — ECB "
            "prints CNY on Chinese holidays and a gap against an unset fix "
            "would be fiction. Onshore only: no licence-clean keyless CNH "
            "source exists (the TMA fixing is licensed), stated rather than "
            "worked around. cross_check_diff is the ECB-vs-BoC derivation "
            "disagreement, published so a silent break in either source is "
            "visible; it is null on rounds where the Bank of Canada leg did "
            "not answer. Economic backdrop, not censorship — deliberately "
            "off the alarm board"
        ),
        "asof": spot["date"],
        **{k: row[k] for k in row if k != "date"},
        # Explicit null when the Bank of Canada leg did not answer: the
        # cross-check exists to make a silent break in either source
        # visible, so its own absence must be visible too.
        "cross_check_diff": row.get("cross_check_diff"),
        "band_pct": 2.0,
        "history_days": len(history),
        "note": (
            "persistent one-sided gaps are the market disputing the state's "
            "price; the fix itself is the policy statement"
        ),
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"cny-fix-gap: {spot['date']} spot {spot['usdcny']} vs fix {fix} "
          f"-> gap {gap_pct}% of fix, {len(history)} days accrued")


if __name__ == "__main__":
    main()
