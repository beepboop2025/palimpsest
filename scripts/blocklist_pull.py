"""Blocklist-archaeology runner — publish readings/blocklist-latest.json.

Diffs consecutive Citizen Lab LINE client blocklists and emits every newly-added keyword as a
NOVELTY observation into the same DDTI / gazetteer pipeline the deletion legs feed.

WHY THIS SIGNAL IS DIFFERENT FROM EVERY OTHER LEG
  The rest of Palimpsest infers sensitivity: a post vanished, so something about it was
  sensitive. Here there is nothing to infer. A keyword shipped inside a censorship client is
  the censor's own trigger list, and a term newly present in version N+1 is a dated directive
  the platform labelled for us. It is the highest-confidence novelty input the system takes.

WHAT IT DOES NOT CLAIM
  The version dates are a BOUND, not the directive timestamp. Citizen Lab's observation of a
  list is an upper bound on when the term was actually ordered, so no latency or velocity is
  derived here — consistent with velocity being suppressed platform-wide.

PUBLISHING SCOPE — a deliberate, conservative choice
  The upstream corpus carries no licence (see scripts/fetch_citizenlab_blocklists.py), so the
  corpus itself is cached under data/ and never redistributed. This reading publishes DERIVED
  values — counts, distributions, the novelty index — plus a bounded sample of newly-added
  terms as evidence, each attributed to Citizen Lab with its source URL. Publishing a handful
  of terms as a cited finding is ordinary research practice and is what makes the reading
  legible; publishing the lists wholesale would be redistribution. EVIDENCE_SAMPLE is the knob.

Gazetteer proposals are REPORTED, never merged. The euphemism gazetteer is human-ratified by
design and this runner does not get to write to it.

    PYTHONPATH=. python3 scripts/fetch_citizenlab_blocklists.py   # acquire first
    PYTHONPATH=. python3 scripts/blocklist_pull.py
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone

from collectors.blocklist_archaeology import (
    BLOCKLIST_ADD, EXHAUSTIVE, BlocklistArchaeologyCollector, BlocklistArtifact,
)
from core.governance import KillSwitch, RateCeiling

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
MANIFEST = os.path.join(ROOT, "data", "citizenlab", "line", "manifest.json")
OUT = os.path.join(READINGS, "blocklist-latest.json")
HIST = os.path.join(READINGS, "blocklist-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see. This driver rewrites
# the reading unconditionally, so it cannot hide a method change behind an
# unchanged number. Carried as provenance: a reader diffing two history rows
# needs to know whether the method moved underneath them.
METHOD_VERSION = 1


# How many newly-added terms ship as evidence in the published reading. Bounded on purpose:
# see the publishing-scope note above.
EVIDENCE_SAMPLE = 12

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _artifacts(manifest: dict) -> list:
    """Build the artifact list in VERSION order. parse_all() sorts by observed_at and Python's
    sort is stable, so version order is what breaks the ties — and there ARE ties: v20/v21 and
    v24/v25 were each committed upstream in the same push, so their date bound is identical
    while their true release order is not."""
    out = []
    for name, f in sorted(manifest["files"].items(), key=lambda kv: kv[1]["order"]):
        if not f.get("observed_at"):
            # An undated artifact cannot be placed in the version sequence, so it cannot be
            # diffed. Skip it loudly rather than guessing a date.
            print(f"  ! {name}: undated, skipped (cannot be ordered)")
            continue
        out.append(BlocklistArtifact(
            app=manifest.get("app", "line"),
            version=f["version"],
            observed_at=datetime.fromisoformat(f["observed_at"].replace("Z", "+00:00")),
            source_ref=os.path.join(ROOT, f["local_path"]),
            completeness=EXHAUSTIVE if manifest.get("completeness") == "exhaustive" else "sampled",
        ))
    return out


def main() -> dict:
    try:
        with open(MANIFEST, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(
            f"FATAL: no usable corpus manifest at {MANIFEST} ({e.__class__.__name__}). "
            "Run scripts/fetch_citizenlab_blocklists.py first — this runner never fetches."
        )

    artifacts = _artifacts(manifest)
    if len(artifacts) < 2:
        raise SystemExit(
            f"FATAL: {len(artifacts)} usable artifact(s) — a diff needs at least two "
            "consecutive versions. Refusing to publish an empty reading."
        )

    collector = BlocklistArchaeologyCollector(
        artifacts,
        kill_switch=KillSwitch(),
        rate_ceiling=RateCeiling(rate=2.0),
    )
    result = collector.run()
    if result.get("status") != "success":
        raise SystemExit(f"FATAL: collector failed — {result.get('error') or result}")

    adds = [o for o in result["observations"] if o["deletion_signal"] == BLOCKLIST_ADD]
    by_version = Counter(o["title"].rsplit("(", 1)[-1].rstrip(")") for o in adds)
    severities = Counter(o["severity"] for o in adds)

    evidence = sorted(
        adds, key=lambda o: (_SEVERITY_RANK.get(o["severity"], 9), o["detected_at"])
    )[:EVIDENCE_SAMPLE]

    reading = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method_version": METHOD_VERSION,
        "title": "Blocklist archaeology — newly-shipped censorship keywords",
        "scope": ("keywords newly present in successive LINE client blocklists; each addition "
                  "is a directive the platform labelled itself, not an inference from deletion"),
        "source": manifest["repo"],
        "attribution": manifest["attribution"],
        "licence_note": manifest["licence"],
        "app": manifest.get("app", "line"),
        "completeness": manifest.get("completeness"),
        "versions_compared": [a.version for a in artifacts],
        "n_versions": len(artifacts),
        "n_additions": len(adds),
        "additions_by_version": dict(sorted(by_version.items())),
        "severity_distribution": dict(severities),
        "n_gazetteer_proposals": result["ledger"]["n_proposals"],
        "gazetteer_proposals": [
            {k: p[k] for k in ("term", "total_support", "association", "score", "state")
             if k in p}
            for p in (result["ledger"].get("proposals") or [])
        ],
        "gazetteer_proposals_note": (
            "candidates for the euphemism gazetteer, REPORTED not merged — the gazetteer is "
            "human-ratified by design and this runner never writes to it. Expect substring "
            "fragments of the same coinage (乌鲁木齐 also yields 乌鲁木/鲁木齐/木齐), which a "
            "ratifier collapses; the count is proposals, not distinct discoveries."),
        # The DDTI attention index is deliberately NOT computed here. It is a recency-weighted
        # live-stream instrument (3-day current window, 2-day half-life) and this corpus is six
        # discrete list versions spanning 174 days in 2014, in which every added term appears
        # exactly once. Attention would be flat and novelty uniformly 1.0, so the ranking would
        # carry no information. Widening the window until it produced a number would be
        # manufacturing one. The gazetteer miner is the correct consumer for this corpus.
        "novelty_index": None,
        "novelty_index_note": (
            "not computed: compute_selectivity_novelty ranks recency-weighted attention over a "
            "live deletion stream, and a decade-old corpus of six discrete version diffs has no "
            "meaningful recency structure — every term is added once. Withheld rather than "
            "computed with stretched windows to yield a number that would not mean anything."),
        "evidence_sample": [
            {"term": o["text"], "version": o["title"].rsplit("(", 1)[-1].rstrip(")"),
             "severity": o["severity"],
             "observed_bound": o["detected_at"].isoformat()
             if hasattr(o["detected_at"], "isoformat") else str(o["detected_at"])}
            for o in evidence
        ],
        "evidence_sample_note": (
            f"a bounded sample of {len(evidence)} of {len(adds)} additions, published as cited "
            "evidence with attribution; the corpus itself is cached locally and not "
            "redistributed by this project"),
        "velocity": None,
        "velocity_note": ("a list observation date is an UPPER BOUND on the directive, not its "
                          "timestamp — no latency is derived from it"),
        "decode_warnings": result["decode_warnings"],
        "skipped": result["skipped"],
        "provenance": {name: {"sha256": f["sha256"], "source_url": f["source_url"],
                              "observed_at": f["observed_at"], "date_basis": f["date_basis"]}
                       for name, f in sorted(manifest["files"].items(),
                                             key=lambda kv: kv[1]["order"])},
    }

    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(reading, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    entry = {k: reading[k] for k in ("generated_at", "n_versions", "n_additions",
                                     "severity_distribution", "n_gazetteer_proposals")}
    with open(HIST, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return reading


if __name__ == "__main__":
    r = main()
    print(f"blocklist archaeology -> {os.path.relpath(OUT, ROOT)}")
    print(f"  {r['n_versions']} versions · {r['n_additions']} newly-shipped keywords · "
          f"{r['n_gazetteer_proposals']} gazetteer proposals")
    print(f"  severity: {r['severity_distribution']}")
    for e in r["evidence_sample"][:5]:
        print(f"    {e['severity']:<6} {e['version']:<10} {e['term']}")
