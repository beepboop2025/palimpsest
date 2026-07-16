"""BLEEDTHROUGH runner — one injector-tomography round, published to
readings/bleedthrough-latest.json (+ history).

UNLIKE the passive signals (OONI, GDELT, DDTI) this one ACTIVELY PROBES China, so it must
NOT run from GitHub Actions or any shared CI IP — those get burned instantly and it is the
wrong place for rotating probers. It is built to run from a DEPLOYMENT-CONTROLLED prober
(a rotating VPS outside China), and it is triple-gated:

  1. env BLEEDTHROUGH_LIVE must be truthy (default OFF — a bare run does nothing),
  2. the kill switch (core/governance) must be released, and
  3. the target file must be a CURATED list, not the shipped placeholder example.

Direct targets ride the direct transport (fleet size); resolver targets ride the
open-resolver fallback (pool / rotation / regional signal that survives inbound decay). A
disk baseline (JsonFleetStore) remembers each vantage across runs so rotation/capacity/
silence events fall out. Honesty guard: if nothing injected this round, abstain rather than
publish a hollow board. Standard-library only.

    BLEEDTHROUGH_LIVE=1 BLEEDTHROUGH_TARGETS=config/bleedthrough_targets.json \\
        python -m scripts.bleedthrough_pull
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.bleedthrough import (
    JsonFleetStore,
    FleetBaselineStore,
    _udp_transport,
    load_targets,
    open_resolver_transport,
    run_round,
)
from core.governance import KillSwitch, RateCeiling

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "bleedthrough-latest.json")
HIST = os.path.join(READINGS, "bleedthrough-history.jsonl")
STORE_DIR = os.path.join(ROOT, "data", "bleedthrough_baselines")

TARGETS = os.getenv("BLEEDTHROUGH_TARGETS", os.path.join(ROOT, "config", "bleedthrough_targets.json"))
RATE_PER_SEC = float(os.getenv("BLEEDTHROUGH_RATE", "5"))   # polite default; deployment tunes
BURST = int(os.getenv("BLEEDTHROUGH_BURST", "24"))


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _refuse(msg: str) -> None:
    print(f"BLEEDTHROUGH: refusing to run — {msg}")


def main() -> None:
    # ── gate 1: explicit opt-in ────────────────────────────────────────────────────────
    if not _truthy(os.getenv("BLEEDTHROUGH_LIVE")):
        _refuse("BLEEDTHROUGH_LIVE is not set. This runner actively probes China and must be "
                "launched deliberately from a controlled prober, never from CI.")
        return
    # ── gate 2: kill switch ────────────────────────────────────────────────────────────
    if KillSwitch().is_halted():
        _refuse("the kill switch is engaged.")
        return
    # ── gate 3: not the placeholder example ────────────────────────────────────────────
    if not os.path.exists(TARGETS):
        _refuse(f"no target file at {TARGETS}. Curate one with curate_dark_ips / "
                f"curate_resolvers; the shipped file is an example only.")
        return
    try:
        raw = json.load(open(TARGETS, encoding="utf-8"))
    except (OSError, ValueError) as e:
        _refuse(f"target file unreadable ({e}).")
        return
    if raw.get("_meta", {}).get("placeholder"):
        _refuse("the target file is the shipped placeholder (RFC 5737 documentation IPs). "
                "Replace it with a curated list before probing.")
        return

    conf = load_targets(TARGETS)
    probe, dark, resolver = conf["probe"], conf["dark"], conf["resolver"]
    rate = RateCeiling(rate=RATE_PER_SEC)
    store = FleetBaselineStore(store=JsonFleetStore(STORE_DIR))

    fingerprints, events = [], []
    # direct-injection round (fleet size) over dark IPs
    if dark:
        r = run_round(probe, dark, transport=_udp_transport, store=store,
                      rate_ceiling=rate, burst=BURST)
        fingerprints += r["fingerprints"]
        events += r["events"]
    # open-resolver fallback round (pool / regional) over live resolvers
    if resolver:
        rt = open_resolver_transport(clean_answers=conf.get("clean_answers"))
        r = run_round(probe, resolver, transport=rt, store=store, rate_ceiling=rate, burst=BURST)
        fingerprints += r["fingerprints"]
        events += r["events"]

    injecting = [fp for fp in fingerprints if fp.pool_hash]
    # Honesty guard: no injection observed anywhere → abstain (channel may be down / list stale)
    if not injecting:
        print("BLEEDTHROUGH: no injection observed on any vantage this round "
              "(channel down / list stale / all silent) — abstaining, not publishing.")
        return

    now = datetime.now(timezone.utc)
    out = {
        "generated_at": now.isoformat(),
        "signal": "bleedthrough",
        "title": "GFW injector fleet",
        "scope": ("apparatus-layer tomography of the Great Firewall's DNS-injector fleet: "
                  "size, forged-IP pools, regional divergence, and operational events"),
        "method": ("the censor as sensor — benign stateless UDP DNS probes provoke the GFW's "
                   "own injectors to answer; we fingerprint the fleet from the forgeries. "
                   "Direct transport = fleet size; open-resolver fallback = pool/regional."),
        "probe_domain": probe.domain,
        "vantages_probed": len(fingerprints),
        "vantages_injecting": len(injecting),
        "distinct_pools": len({fp.pool_hash for fp in injecting}),
        "max_process_count": max((fp.process_count for fp in injecting), default=0),
        "events": [{"kind": e.kind, "vantage": e.vantage_tag, "detail": e.detail,
                    "severity": e.severity()} for e in events],
    }

    os.makedirs(READINGS, exist_ok=True)
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
    sig_keys = ("vantages_injecting", "distinct_pools", "max_process_count", "events")
    if not prev or any(prev.get(k) != out.get(k) for k in sig_keys):
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "vantages_injecting": out["vantages_injecting"],
                "distinct_pools": out["distinct_pools"],
                "max_process_count": out["max_process_count"],
                "n_events": len(events),
            }, ensure_ascii=False) + "\n")

    print(f"=== BLEEDTHROUGH — {len(injecting)}/{len(fingerprints)} vantages injecting, "
          f"{out['distinct_pools']} distinct pools, max processes {out['max_process_count']} ===")
    for e in events:
        print(f"  [{e.severity()}] {e.kind} — {e.vantage_tag}: {e.detail}")


if __name__ == "__main__":
    main()
