"""BLEEDTHROUGH target-list curation — one command to stand up the prober's target list.

Reads a per-province PREFIX config (config/bleedthrough_prefixes.json), samples candidate IPs
from each Chinese CIDR, classifies each into a dark IP (direct transport) or a live open
resolver (open-resolver fallback), and writes the curated config/bleedthrough_targets.json
that scripts.bleedthrough_pull consumes.

This step touches the network — but only with BENIGN, UNcensored control-domain DNS queries
to test liveness / dark-space (never a censored probe). It is still gated like the runner
because it hits real Chinese IPs and should run from the same controlled, rotating prober:

  1. env BLEEDTHROUGH_LIVE must be truthy,
  2. the kill switch must be released, and
  3. the prefix file must be a real list, not the shipped placeholder example.

Rate-ceiling bounded and rng-seeded (BLEEDTHROUGH_SEED) for reproducibility. Stdlib only.

    BLEEDTHROUGH_LIVE=1 BLEEDTHROUGH_PREFIXES=config/bleedthrough_prefixes.json \\
        python -m scripts.bleedthrough_curate
"""
from __future__ import annotations

import json
import os
import random
import tempfile
from pathlib import Path

from collectors.bleedthrough import _dns_exchange, build_target_file
from core.governance import KillSwitch, RateCeiling

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIXES = os.getenv("BLEEDTHROUGH_PREFIXES", os.path.join(ROOT, "config", "bleedthrough_prefixes.json"))
OUT = os.getenv("BLEEDTHROUGH_TARGETS", os.path.join(ROOT, "config", "bleedthrough_targets.json"))
RATE_PER_SEC = float(os.getenv("BLEEDTHROUGH_RATE", "5"))
WAIT = float(os.getenv("BLEEDTHROUGH_WAIT", "1.2"))


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: str, value: dict) -> None:
    """Commit the curated target set without exposing a partial JSON document."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _refuse(msg: str) -> None:
    print(f"BLEEDTHROUGH curate: refusing to run — {msg}")


def main() -> None:
    if not _truthy(os.getenv("BLEEDTHROUGH_LIVE")):
        _refuse("BLEEDTHROUGH_LIVE is not set. Curation queries real Chinese IPs (benignly) "
                "and must be launched deliberately from a controlled prober, never from CI.")
        return
    if KillSwitch().is_halted():
        _refuse("the kill switch is engaged.")
        return
    if not os.path.exists(PREFIXES):
        _refuse(f"no prefix file at {PREFIXES}. Copy the example and fill in real per-province "
                f"Chinese prefixes.")
        return
    try:
        conf = json.load(open(PREFIXES, encoding="utf-8"))
    except (OSError, ValueError) as e:
        _refuse(f"prefix file unreadable ({e}).")
        return
    if conf.get("_meta", {}).get("placeholder"):
        _refuse("the prefix file is the shipped placeholder (RFC 5737 documentation ranges). "
                "Replace it with real prefixes before curating.")
        return

    rate = RateCeiling(rate=RATE_PER_SEC)
    seed = os.getenv("BLEEDTHROUGH_SEED")
    rng = random.Random(int(seed)) if seed is not None else random.Random()

    kill = KillSwitch()

    def exchange(domain, ip):
        kill.require_live()                   # halt mid-curation, not just at startup
        rate.acquire()                        # one token per benign control query
        return _dns_exchange(domain, ip, wait=WAIT)

    out = build_target_file(conf, exchange=exchange, rng=rng)
    dark = sum(1 for t in out["targets"] if t.get("kind") == "dark")
    resolver = sum(1 for t in out["targets"] if t.get("kind") == "resolver")

    if not out["targets"]:
        _refuse("no usable targets found (all candidates were unresponsive or non-resolvers). "
                "Widen the prefixes or raise sample_per_prefix; not writing an empty list.")
        return

    _atomic_write_json(OUT, out)

    by_prov = {}
    for t in out["targets"]:
        by_prov.setdefault(t["province"], [0, 0])
        by_prov[t["province"]][0 if t["kind"] == "dark" else 1] += 1
    print(f"=== BLEEDTHROUGH targets curated → {OUT} ===")
    print(f"  {len(out['targets'])} targets: {dark} dark + {resolver} resolver")
    for prov, (d, r) in sorted(by_prov.items()):
        print(f"    {prov:<8} {d} dark · {r} resolver")
    print("  next: BLEEDTHROUGH_LIVE=1 python -m scripts.bleedthrough_pull")


if __name__ == "__main__":
    main()
