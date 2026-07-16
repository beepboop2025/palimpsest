"""BLEEDTHROUGH demo reading — writes a CLEARLY-BADGED illustrative
readings/bleedthrough-latest.json so the signal page is demonstrable before a live prober
is deployed.

Integrity: this is not fake live data. It runs the REAL run_round pipeline over CANNED
transports (the Wallbleed multi-injector model), and stamps `"demo": true` so the page shows
a DEMO badge. The first real round from a controlled prober overwrites it with `demo` absent.
No network, standard-library only.

    python -m scripts.bleedthrough_demo
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.bleedthrough import (
    FleetBaselineStore,
    InjectorProbe,
    RawInjection,
    TargetVantage,
    run_round,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "bleedthrough-latest.json")
HIST = os.path.join(READINGS, "bleedthrough-history.jsonl")

NATIONAL = ["4.36.66.178", "8.7.198.45", "59.24.3.173"]   # the national GFW forged-IP pool
HENAN = ["1.2.3.4", "5.6.7.8"]                              # a divergent provincial pool


def _transport(pool_by_ip, n_by_ip):
    """Canned transport: each vantage's injectors cycle its pool independently (Wallbleed
    model). n_by_ip sets how many parallel injectors answer (the process-count signal)."""
    state = {}

    def _t(domain, ip):
        k = state.get(ip, 0)
        state[ip] = k + 1
        pool = pool_by_ip[ip]
        return [RawInjection(pool[(k + off) % len(pool)], rr_ttl=64)
                for off in range(n_by_ip[ip])]
    return _t


def main() -> None:
    probe = InjectorProbe(domain="torproject.org", ddti="CIRCUMVENTION")
    vantages = [
        TargetVantage("shanghai.dark",  "CN-SH", "AS4812"),
        TargetVantage("beijing.dark",   "CN-BJ", "AS4808"),
        TargetVantage("guangdong.dark", "CN-GD", "AS4134"),
        TargetVantage("henan.dark.1",   "CN-HA", "AS4837"),
        TargetVantage("henan.dark.2",   "CN-HA", "AS4837"),
    ]
    pools = {"shanghai.dark": NATIONAL, "beijing.dark": NATIONAL, "guangdong.dark": NATIONAL,
             "henan.dark.1": HENAN, "henan.dark.2": HENAN}

    store = FleetBaselineStore()
    # round 1 — seed the baseline (Beijing at 2 injectors); no events on first sight
    run_round(probe, vantages, store=store, burst=12,
              transport=_transport(pools, {ip: 2 for ip in pools}))
    # round 2 — Beijing scales to 3 injectors (capacity_shift); Henan pool diverges from the
    # national baseline (regional_firewall ×2). This is the reading we publish.
    r = run_round(probe, vantages, store=store, burst=12,
                  transport=_transport(pools, {**{ip: 2 for ip in pools}, "beijing.dark": 3}))
    sig = r["signal"]

    now = datetime.now(timezone.utc)
    out = {
        "generated_at": now.isoformat(),
        "demo": True,
        "signal": "bleedthrough",
        "title": "GFW injector fleet",
        "scope": ("apparatus-layer tomography of the Great Firewall's DNS-injector fleet: "
                  "size, forged-IP pools, regional divergence, and operational events"),
        "method": ("the censor as sensor — benign stateless UDP DNS probes provoke the GFW's "
                   "own injectors to answer; we fingerprint the fleet from the forgeries. "
                   "Direct transport = fleet size; open-resolver fallback = pool/regional."),
        "probe_domain": probe.domain,
        "vantages_probed": sig["vantages_probed"],
        "vantages_injecting": sig["vantages_injecting"],
        "distinct_pools": sig["distinct_pools"],
        "max_process_count": sig["max_process_count"],
        "events": sig["events"],
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "generated_at": out["generated_at"], "demo": True,
            "vantages_injecting": out["vantages_injecting"],
            "distinct_pools": out["distinct_pools"],
            "max_process_count": out["max_process_count"],
            "n_events": len(out["events"]),
        }, ensure_ascii=False) + "\n")

    print(f"wrote DEMO reading: {out['vantages_injecting']}/{out['vantages_probed']} injecting, "
          f"{out['distinct_pools']} pools, {len(out['events'])} events")
    for e in out["events"]:
        print(f"  [{e['severity']}] {e['kind']} — {e['vantage']}: {e['detail']}")


if __name__ == "__main__":
    main()
