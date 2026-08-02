#!/usr/bin/env python3
"""Seal every published reading into an append-only, hash-chained ledger.

Why this exists
---------------
Palimpsest's readings are regenerated in place. `readings/ooni-gfw-latest.json`
holds today's numbers and yesterday's are gone: the file is a live view, not a
record. The observatory's whole thesis is that we measure what gets removed or
rewritten and seal our own record so it cannot be quietly rewritten in turn,
and until now that seal covered four sources out of thirty-one.

The other twenty-seven were unattested. Anyone, including us, could have
changed what a past reading said and nothing would have contradicted it. As
readers shift from people to agents this stops being theoretical: an agent
citing a reading has no way to check that the number it quotes is the number
we published, and "trust the site" is exactly the posture this project exists
to reject.

This sweep closes that. Each run seals the current bytes of every reading into
a chain, so a stranger can later prove what we published and when.

Why a sweep and not a call in each driver
-----------------------------------------
There are thirty-one drivers and no shared write helper; each one dumps its own
JSON. Editing all of them would be thirty-one chances to get it wrong, and
`tests/test_method_version_guard.py` detects unconditional writes by matching
the literal `with open(OUT, "w"` at four-space indentation, so routing them
through a helper would break that guard. A sweep needs no driver changes, and
`append_seal` is already idempotent, so re-running is free.

Why its own ledger
------------------
`readings/erasure-ledger.jsonl` is not a general-purpose chain. `erasure_pull`
publishes `ledger_entries` and `merkle_root` taken from that ledger's summary
into the Erasure Observatory reading and its history, so those numbers mean
"the erasure record's own seal". Adding twenty-seven unrelated sources would
silently redefine a published field. This writes to its own chain instead, and
`anchor_roots.py` anchors both, so nothing loses its Bitcoin anchor.

The two chains overlap on four sources by design: the erasure ledger keeps its
own attestation of its own inputs, and this one carries the complete record of
everything the observatory publishes. Two independent seals of the same
artifact is a stronger claim than one, not a conflict.

Usage
-----
    python3 scripts/seal_readings.py            # seal whatever changed
    python3 scripts/seal_readings.py --check    # verify only, no writes (CI)
    python3 scripts/seal_readings.py --coverage # which readings are sealed

Exit codes: 0 all good, 1 a reading could not be read or the chain is broken.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import sealed_ledger as led  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
LEDGER = os.path.join(READINGS, "readings-ledger.jsonl")

# Files under readings/ that are not themselves readings: the chains, the
# append-only history rows, and the transcript corpora the chains commit to.
#
# anchors-latest.json is here for the same reason anchors.jsonl is: it is the
# anchor log's own summary, a record ABOUT the chains rather than a measurement.
# Sweeping it would also make this sweep self-feeding, because the seal step
# runs before the anchor step and anchors-latest.json takes a fresh ts on every
# anchor, so each run would seal the previous run's anchor record and the chain
# would grow on every run for ever.
_NOT_A_READING = {
    "readings-ledger.jsonl", "erasure-ledger.jsonl", "eval-registry.jsonl",
    "anchors.jsonl", "anchors-latest.json",
}

# The Generative Firewall Index predates the -latest.json convention and is
# published at readings/latest.json. It is already sealed into the erasure
# ledger under this name; keep the name so the two chains agree on what to
# call the same source.
_SPECIAL = {"latest.json": "generative-firewall"}


def discover() -> list[tuple[str, str]]:
    """Return (source_name, path) for every published reading, sorted."""
    found: dict[str, str] = {}
    for path in glob.glob(os.path.join(READINGS, "*-latest.json")):
        base = os.path.basename(path)
        if base in _NOT_A_READING:
            continue
        found[base[: -len("-latest.json")]] = path
    for base, source in _SPECIAL.items():
        path = os.path.join(READINGS, base)
        if os.path.exists(path):
            found[source] = path
    return sorted(found.items())


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def seal_all(dry_run: bool = False) -> tuple[int, int, list[str]]:
    """Seal every reading. Returns (n_sealed, n_unchanged, problems)."""
    sealed = unchanged = 0
    problems: list[str] = []
    for source, path in discover():
        try:
            reading = _load(path)
        except (OSError, ValueError) as exc:
            # Fail loud per reading but keep going: one malformed file must not
            # stop the rest of the record being sealed.
            problems.append(f"{source}: cannot read ({exc})")
            continue
        if dry_run:
            digest = led.payload_digest(reading)
            entries = led.read_ledger(LEDGER)
            latest = next((e for e in reversed(entries)
                           if e.get("source") == source), None)
            if latest and latest.get("payload_sha256") == digest:
                unchanged += 1
            else:
                sealed += 1
            continue
        entry = led.append_seal(LEDGER, source, reading)
        if entry is None:
            unchanged += 1
        else:
            sealed += 1
    return sealed, unchanged, problems


def _newest_seal(entries: list[dict]) -> dict[str, str]:
    """source -> payload digest of its most recent seal."""
    newest: dict[str, str] = {}
    for e in entries:
        source = e.get("source")
        if source:
            newest[source] = e.get("payload_sha256")
    return newest


def coverage() -> dict:
    """What is sealed, what is not, whether the chain verifies, and what drifted.

    `drifted` names sources whose newest seal no longer matches the bytes on
    disk. Some drift is the normal cost of a snapshot cadence: a reading its own
    workflow refreshed is unsealed until the next sweep. It is reported rather
    than asserted for that reason, but it has to be visible, because the one
    thing that must never happen is the opposite case, a seal recorded against
    bytes the site never served at the timestamp the entry claims. Append-only
    means such an entry can never be withdrawn, so run this before committing a
    freshly generated chain: everything should be sealed against the tree that
    is actually published.
    """
    entries = led.read_ledger(LEDGER)
    ok, chain_problems = led.verify(entries)
    sealed_sources = {e.get("source") for e in entries}
    newest = _newest_seal(entries)
    published = discover()
    all_sources = {s for s, _ in published}
    drifted, unreadable = [], []
    for source, path in published:
        try:
            digest = led.payload_digest(_load(path))
        except (OSError, ValueError):
            unreadable.append(source)
            continue
        if newest.get(source) != digest:
            drifted.append(source)
    return {
        "ledger": os.path.relpath(LEDGER, ROOT),
        "entries": len(entries),
        "verified": ok,
        "problems": chain_problems,
        "readings_published": len(all_sources),
        "readings_sealed": len(all_sources & sealed_sources),
        "unsealed": sorted(all_sources - sealed_sources),
        "drifted": sorted(drifted),
        "unreadable": sorted(unreadable),
        "merkle_root": led.merkle_root(entries) if entries else None,
        "head_hash": entries[-1]["entry_hash"] if entries else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="verify the chain and report what would be sealed; no writes")
    ap.add_argument("--coverage", action="store_true",
                    help="print the coverage report as JSON and exit")
    args = ap.parse_args()

    if args.coverage:
        print(json.dumps(coverage(), indent=1))
        return 0

    entries = led.read_ledger(LEDGER)
    ok, chain_problems = led.verify(entries)
    if not ok:
        # Never extend a chain that no longer verifies: appending onto a broken
        # history would launder the break behind new, valid-looking links.
        print(f"REFUSING TO SEAL: {LEDGER} does not verify", file=sys.stderr)
        for p in chain_problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    sealed, unchanged, problems = seal_all(dry_run=args.check)
    verb = "would seal" if args.check else "sealed"
    print(f"{verb} {sealed}, unchanged {unchanged}, "
          f"chain now {len(led.read_ledger(LEDGER))} entries")
    for p in problems:
        print(f"  PROBLEM {p}", file=sys.stderr)
    cov = coverage()
    if cov["unsealed"]:
        print(f"  still unsealed: {', '.join(cov['unsealed'])}", file=sys.stderr)
    if cov["drifted"]:
        print(f"  published bytes differ from their newest seal: "
              f"{', '.join(cov['drifted'])}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
