"""BLEEDTHROUGH prefix fetcher — build a REAL per-province prefix list from public BGP data.

Reads a seed ASN->province map (config/bleedthrough_asns.json), fetches each ASN's announced
prefixes from RIPEstat, samples a handful of routable IPv4 blocks per ASN, and writes the real
config/bleedthrough_prefixes.json that scripts.bleedthrough_curate consumes.

SAFE ANYWHERE: this contacts RIPEstat (public BGP data in Europe), never China, and reveals no
probing intent — so unlike curate/pull it is NOT gated and may run on the laptop or the prober.
Only the later curate/pull steps touch China and stay prober-gated. rng-seedable
(BLEEDTHROUGH_SEED) for reproducibility. Standard-library only (urllib).

    python -m scripts.bleedthrough_fetch_prefixes
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import time
import urllib.request
from pathlib import Path

from collectors.bleedthrough import build_prefix_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASNS = os.getenv("BLEEDTHROUGH_ASNS", os.path.join(ROOT, "config", "bleedthrough_asns.json"))
OUT = os.getenv("BLEEDTHROUGH_PREFIXES", os.path.join(ROOT, "config", "bleedthrough_prefixes.json"))
RIPESTAT = "https://stat.ripe.net/data/announced-prefixes/data.json?resource="
UA = "palimpsest.info observatory (Bleedthrough prefix build; contact desk@palimpsest.info)"
THROTTLE = float(os.getenv("BLEEDTHROUGH_FETCH_THROTTLE", "1.0"))


def _fsync_directory(path: Path) -> None:
    """Persist the rename where the host filesystem supports directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Some filesystems (and macOS test volumes) do not fsync directories.
            pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: str, value: dict) -> None:
    """Publish one complete private prefix snapshot or leave the old one intact."""
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


def _ripestat_fetch(asn: str) -> dict:
    """One RIPEstat announced-prefixes call. Fail-soft: returns {} on any error so a flaky ASN
    is skipped rather than aborting the whole build. Polite throttle between calls."""
    url = RIPESTAT + urllib.request.quote(str(asn))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read(16 * 1024 * 1024)
        time.sleep(THROTTLE)
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001 — deliberately broad: any failure => skip this ASN
        print(f"  ! {asn}: fetch failed ({type(e).__name__}) — skipping")
        return {}


def main() -> None:
    try:
        conf = json.load(open(ASNS, encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"BLEEDTHROUGH fetch: cannot read ASN map {ASNS} ({e})")
        return
    entries = conf.get("asns", [])
    if not entries:
        print("BLEEDTHROUGH fetch: ASN map has no `asns` — nothing to do")
        return

    seed = os.getenv("BLEEDTHROUGH_SEED")
    rng = random.Random(int(seed)) if seed is not None else random.Random()

    out = build_prefix_config(
        entries, fetch=_ripestat_fetch, rng=rng,
        prefixes_per_asn=int(conf.get("prefixes_per_asn", 6)),
        probe=conf.get("probe"), control_domain=conf.get("control_domain", "example.com"),
        clean_answers=conf.get("clean_answers"),
        sample_per_prefix=int(conf.get("sample_per_prefix", 6)),
        min_len=int(conf.get("min_prefix_len", 16)), max_len=int(conf.get("max_prefix_len", 24)),
    )
    if not out["provinces"]:
        print("BLEEDTHROUGH fetch: no prefixes resolved for any ASN — not writing an empty list")
        return

    _atomic_write_json(OUT, out)

    total = sum(len(p["prefixes"]) for p in out["provinces"])
    print(f"=== BLEEDTHROUGH prefixes → {OUT} ===")
    print(f"  {total} real IPv4 prefixes across {len(out['provinces'])} ASN/province groups")
    for p in out["provinces"]:
        print(f"    {p['province']:<7} {p['asn']:<8} {len(p['prefixes'])} prefixes  ({p.get('provider','')})")
    print("  next (on a prober OUTSIDE China): BLEEDTHROUGH_LIVE=1 python -m scripts.bleedthrough_curate")


if __name__ == "__main__":
    main()
