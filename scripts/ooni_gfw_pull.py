"""OONI Great-Firewall signal runner — pull CN network-censorship from OONI's
public aggregation API and publish readings/ooni-gfw-latest.json.

Vantage-insensitive (ingests already-aggregated OONI open data; probes nothing
itself), key-less, standard-library only. Mirrors the GDELT/DDTI pull pattern:
collect -> honesty-guard/abstain -> write-if-changed -> append history.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from collectors.ooni_gfw import test_signals, top_blocked_domains

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "ooni-gfw-latest.json")
HIST = os.path.join(READINGS, "ooni-gfw-history.jsonl")

WINDOW_DAYS = 7   # CN measurement volume is uneven; a 7d window keeps the rate stable


def _reading(test: dict) -> str:
    if not test.get("available"):
        return "no CN measurements in the window — abstaining for this test"
    rate = test.get("anomaly_rate")
    if rate is None:
        return "measurements failed to complete — no rate"
    pct = rate * 100
    if pct >= 60:
        return f"{pct:.0f}% anomalous — heavily interfered with"
    if pct >= 25:
        return f"{pct:.0f}% anomalous — substantial interference"
    if pct >= 5:
        return f"{pct:.0f}% anomalous — some interference"
    return f"{pct:.0f}% anomalous — largely reachable"


def main() -> None:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    until = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    tests = test_signals(since, until)
    usable = [t for t in tests if t.get("available")]

    # Honesty guard: if OONI answered for no test at all (unreachable / rate-
    # limited / genuinely no CN data), abstain rather than publish a hollow board.
    if not usable:
        print("OONI returned no usable CN measurements for any test "
              "(unreachable / rate-limited / no data) — abstaining, not publishing")
        return

    top = top_blocked_domains(since, until)

    # Overall GFW pressure: measurement-weighted mean anomaly rate across the
    # tests that had data, scaled to 0-100. Weighting by measurement count stops
    # a thinly-measured test from dominating.
    tot_valid = sum((t["measurement_count"] - t["failure_count"]) for t in usable)
    tot_anom = sum(t["anomaly_count"] for t in usable)
    gfw_index = round(100 * tot_anom / tot_valid, 1) if tot_valid > 0 else None

    for t in tests:
        t["reading"] = _reading(t)

    out = {
        "generated_at": now.isoformat(),
        "source": "OONI aggregation API (api.ooni.io), probe_cc=CN",
        "scope": ("live Great Firewall network blocking — website, messenger and "
                  "circumvention-tool reachability, measured inside China by OONI Probe"),
        "method": ("side-channel: we ingest OONI's already-aggregated open data; "
                   "we never probe a censored resource ourselves (vantage-insensitive)"),
        "china_caveat": ("the GFW blocks via RST/DNS injection without a block page, so "
                         "OONI 'confirmed' is near-zero for CN; we track anomaly rate, not confirmed"),
        "window_days": WINDOW_DAYS,
        "since": since, "until": until,
        "gfw_index": gfw_index,
        "n_measurements": sum(t["measurement_count"] for t in usable),
        "n_tests_with_data": len(usable),
        "tests": tests,
        "top_blocked": top,
    }
    os.makedirs(READINGS, exist_ok=True)

    # write-if-changed on the substantive fields (ignore the timestamp) so an
    # unchanged board doesn't churn a commit every run
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
    sig_keys = ("gfw_index", "n_tests_with_data", "tests", "top_blocked")
    changed = any(prev.get(k) != out.get(k) for k in sig_keys)
    if changed or not prev:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "gfw_index": gfw_index,
                "n_measurements": out["n_measurements"],
                "n_tests_with_data": out["n_tests_with_data"],
                "top_blocked": (top[0]["domain"] if top else None),
            }, ensure_ascii=False) + "\n")

    print(f"=== OONI GFW signal — index {gfw_index} "
          f"({out['n_measurements']} CN measurements, {len(usable)}/{len(tests)} tests) ===")
    for t in tests:
        print(f"  {t['test']:<18} {t.get('reading')}")
    if top:
        print("  top blocked:")
        for d in top[:8]:
            print(f"    {d['domain'][:40]:<40} {d['anomaly_rate']*100:.0f}% ({d['measurement_count']} probes)")


if __name__ == "__main__":
    main()
