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
    REGIONAL_FIREWALL,
    _udp_transport,
    distinct_region_pools,
    load_targets,
    open_resolver_transport,
    run_round,
)
from core.claim_support import looks_sampled
from core.governance import KillSwitch, RateCeiling

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "bleedthrough-latest.json")
HIST = os.path.join(READINGS, "bleedthrough-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see, even if the numbers
# do not move. Write-if-changed compares readings, so without this a methodology
# correction that leaves the values identical never reaches the published file and
# the site keeps asserting a method it no longer uses.
METHOD_VERSION = 1

STORE_DIR = os.path.join(ROOT, "data", "bleedthrough_baselines")

TARGETS = os.getenv("BLEEDTHROUGH_TARGETS", os.path.join(ROOT, "config", "bleedthrough_targets.json"))
RATE_PER_SEC = float(os.getenv("BLEEDTHROUGH_RATE", "5"))   # polite default; deployment tunes
BURST = int(os.getenv("BLEEDTHROUGH_BURST", "24"))
WAIT_S = float(os.getenv("BLEEDTHROUGH_WAIT", "1.2"))       # listen window per query, recorded

# Provenance of the prober itself. Deliberately COARSE: the exact host is operator-only. A
# reading that named the box would bind this prober to the public api.seiche.info A record,
# which is exactly the linkage ops/bleedthrough_prober.sh refuses by default.
VANTAGE_KIND = os.getenv("BLEEDTHROUGH_VANTAGE_KIND", "single fixed-IP VPS outside China")
VANTAGE_COUNTRY = os.getenv("BLEEDTHROUGH_VANTAGE_COUNTRY") or None

# A round is one prober, however many targets it probes. Recorded so no reader can mistake
# the target count for a count of observation points.
VANTAGE_COUNT = 1


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _code_version() -> str | None:
    """Best-effort commit id, read straight off .git — no subprocess (a flagged sink), and
    None when the prober runs from an exported tree with no .git, which is honest."""
    git = os.path.join(ROOT, ".git")
    try:
        head = open(os.path.join(git, "HEAD"), encoding="utf-8").read().strip()
        if not head.startswith("ref:"):
            return head[:40] or None
        ref = head.split(":", 1)[1].strip()
        try:
            return open(os.path.join(git, ref), encoding="utf-8").read().strip()[:40] or None
        except OSError:
            for line in open(os.path.join(git, "packed-refs"), encoding="utf-8"):
                if line.rstrip().endswith(" " + ref):
                    return line.split(" ", 1)[0][:40]
    except OSError:
        return None
    return None


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
    # Armed PER PROBE, not merely checked once above: a round is thousands of datagrams over
    # roughly an hour, and a startup-only gate leaves that whole window unstoppable.
    kill = KillSwitch()
    store = FleetBaselineStore(store=JsonFleetStore(STORE_DIR))

    fingerprints, events = [], []
    # direct-injection round (fleet size) over dark IPs
    if dark:
        r = run_round(probe, dark, transport=_udp_transport, store=store,
                      kill_switch=kill, rate_ceiling=rate, burst=BURST)
        fingerprints += r["fingerprints"]
        events += r["events"]
    # open-resolver fallback round (pool / regional) over live resolvers
    if resolver:
        rt = open_resolver_transport(clean_answers=conf.get("clean_answers"))
        r = run_round(probe, resolver, transport=rt, store=store,
                      kill_switch=kill, rate_ceiling=rate, burst=BURST)
        fingerprints += r["fingerprints"]
        events += r["events"]

    injecting = [fp for fp in fingerprints if fp.pool_hash]
    # Honesty guard 1: no injection observed anywhere → abstain (channel may be down / list stale)
    if not injecting:
        print("BLEEDTHROUGH: no injection observed on any target this round "
              "(channel down / list stale / all silent) — abstaining, not publishing.")
        return

    # Honesty guard 2: when per-target pool hashes are near-unique, the censor's forged-IP
    # pool is being SAMPLED rather than enumerated at this burst. Per-target pools are then
    # not comparable to each other and any regional reading is sampling noise, so strip the
    # regional claims and record that we did. This is the failure mode a single prober over
    # many dark targets actually produces; regional_divergence guards it too, and this is the
    # second layer in case a future caller bypasses that one.
    sampled_pools = looks_sampled(len({fp.pool_hash for fp in injecting}), len(injecting))
    if sampled_pools:
        dropped = sum(1 for e in events if e.kind == REGIONAL_FIREWALL)
        events = [e for e in events if e.kind != REGIONAL_FIREWALL]
        print(f"BLEEDTHROUGH: pool hashes are near-unique across targets — the pool is "
              f"sampled, not enumerated, at burst {BURST}; regional divergence is not "
              f"identifiable this round. Dropped {dropped} regional event(s).")

    # transports: `ran: False` with a null count, never 0 — "did not run" is not "measured zero"
    transports = {
        "direct": {"ran": bool(dark), "targets": len(dark) or None},
        "open_resolver": {"ran": bool(resolver), "targets": len(resolver) or None},
    }
    legs = [name for name, key in (("direct injection over dark IPs", "direct"),
                                   ("open-resolver fallback", "open_resolver"))
            if transports[key]["ran"]]

    now = datetime.now(timezone.utc)
    out = {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "signal": "bleedthrough",
        "title": "GFW injector fleet",
        "scope": ("apparatus-layer tomography of the Great Firewall's DNS-injector fleet: "
                  "forged-IP pools, a floor on parallel injector responses, and operational "
                  "events, measured from outside China"),
        # Computed from what actually ran this round — a hardcoded sentence would misdescribe
        # the first round whose transport mix differs.
        "method": ("the censor as sensor — benign stateless UDP DNS probes provoke the GFW's "
                   "own injectors to answer; we fingerprint the fleet from the forgeries. "
                   f"Transport this round: {' + '.join(legs)}. "
                   "Injector count is a FLOOR, not a census: each injector answers a given "
                   "query at most once, so a silent injector is undercounted."),
        "probe_domain": probe.domain,
        # NB: these count TARGET IPs probed, not observation points. There is one prober;
        # see provenance.vantage_count.
        "vantages_probed": len(fingerprints),
        "vantages_injecting": len(injecting),
        "distinct_pools": distinct_region_pools(injecting),
        "distinct_pools_basis": "union of forged IPs per region, not per-target samples",
        "max_process_count": max((fp.process_count for fp in injecting), default=0),
        "process_count_semantics": "floor",
        "pool_sampling_suspected": sampled_pools,
        "provenance": {
            "vantage_count": VANTAGE_COUNT,
            "vantage_kind": VANTAGE_KIND,
            "vantage_country": VANTAGE_COUNTRY,
            "flow_id_policy": "ephemeral source port per query (paths not pinned)",
            "burst": BURST,
            "rate_per_sec": RATE_PER_SEC,
            "wait_s": WAIT_S,
            "queries_attempted": sum(fp.n_probes for fp in fingerprints),
            "transports": transports,
            "code_version": _code_version(),
            "caveat": ("single-vantage round: observed censorship varies with network path, "
                       "so cross-region comparisons from one prober are not identifiable "
                       "(cf. arXiv:2406.19304)."),
        },
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
    sig_keys = ("vantages_injecting", "distinct_pools", "max_process_count",
                "pool_sampling_suspected", "events")
    # method_version is part of the comparison so a methodology correction reaches
    # the published file even when every value is identical.
    changed = (any(prev.get(k) != out.get(k) for k in sig_keys)
               or prev.get("method_version") != METHOD_VERSION)

    # "When did we last look" and "when did the answer last move" are different
    # questions, and a reader has to be able to tell them apart. Write-if-changed
    # answers only the second, so a fleet that holds still — which is exactly what
    # a stable injector deployment looks like — stopped refreshing generated_at,
    # and the observatory ended up labelling its own healthy signal stale. A quiet
    # apparatus and a dead prober are not the same claim. So every round that gets
    # past the honesty guards publishes its own observation time, and
    # last_changed_at carries the movement. The history file stays gated on change,
    # so the movement record never fills with heartbeats and no false sense of
    # rotation is manufactured. Falling back to the previous generated_at lets a
    # file published before this field existed backfill honestly.
    out["last_changed_at"] = (
        out["generated_at"] if (changed or not prev)
        else (prev.get("last_changed_at") or prev.get("generated_at")))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if changed or not prev:
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                # denominator alongside the numerator, so a later baseline cannot compare
                # rounds of different sizes as though they were the same measurement
                "vantages_probed": out["vantages_probed"],
                "vantages_injecting": out["vantages_injecting"],
                "distinct_pools": out["distinct_pools"],
                "max_process_count": out["max_process_count"],
                "pool_sampling_suspected": out["pool_sampling_suspected"],
                "vantage_count": VANTAGE_COUNT,
                "burst": BURST,
                "n_events": len(events),
            }, ensure_ascii=False) + "\n")
    else:
        print(f"BLEEDTHROUGH: fleet unchanged since {out['last_changed_at']} — "
              f"republished with this round's observation time, history untouched")

    print(f"=== BLEEDTHROUGH — {len(injecting)}/{len(fingerprints)} target IPs injecting "
          f"from {VANTAGE_COUNT} prober, {out['distinct_pools']} distinct pool(s), "
          f"injector floor >={out['max_process_count']} ===")
    for e in events:
        print(f"  [{e.severity()}] {e.kind} — {e.vantage_tag}: {e.detail}")


if __name__ == "__main__":
    main()
