"""Wayback Reconstruction runner — recover deletion / silent-redaction events, with real
archive-witnessed timestamps, for a watchlist of public Chinese URLs, and publish them.

The Internet Archive is an outside-the-wall, retroactive in-country observer: its CDX timeline
brackets a takedown or a state rewrite far tighter than live polling can, with a permanent citable
snapshot on each side. This runner walks each watched URL's timeline
(collectors/wayback_vantage.py), emits the transitions, and writes:

  readings/wayback-latest.json   — one reconstruction per URL (deletion bracket / mutation /
                                    stable / no_baseline / unreachable), plus the DDTI observations
                                    those transitions map to (the SAME adapter every surface uses).
  readings/wayback-history.jsonl — compact append-only time-series of the run.

Honesty guards (fail loud, never a false zero):
  * the deletion moment is only known to within the capture bracket, so velocity is published as
    that explicit [last_live .. first_gone] bracket, never a false-precise instant;
  * a URL the Archive never captured live is `no_baseline`, kept distinct from a real 200→404;
  * if CDX is unreachable for EVERY URL, the runner abstains rather than publish a hollow signal.

Governance-gated (kill switch + rate ceiling) and read-only against the Internet Archive alone —
never Chinese infrastructure, never a person. Standard-library only.

Usage:  PYTHONPATH=. python -m scripts.wayback_reconstruct_pull
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.undertext import DELETION, MUTATION, divergence_to_observation
from collectors.wayback_vantage import WaybackVantagePoint, default_cdx_fetch

try:
    from core.governance import KillSwitch, RateCeiling
except Exception:  # pragma: no cover - governance is always present, but stay fail-soft
    KillSwitch = RateCeiling = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
WATCHLIST = os.path.join(ROOT, "config", "wayback_watchlist.json")
OUT = os.path.join(READINGS, "wayback-latest.json")
HIST = os.path.join(READINGS, "wayback-history.jsonl")

# Polite by construction: the CDX API is a shared public good. One request per URL, well under 1/s.
_RATE_PER_SEC = 0.5
_BURST = 2.0


def load_watchlist() -> list:
    doc = json.load(open(WATCHLIST, encoding="utf-8"))
    return [e for e in doc.get("watchlist", []) if e.get("url")]


def _reconstruction_row(rec) -> dict:
    """The human-facing evidence for one URL (mirrors github-refuge's per-repo reading rows)."""
    primary = rec.primary
    row = {
        "url": rec.url,
        "term": rec.term,
        "n_captures": rec.n_captures,
        "first_capture": rec.first_capture,
        "last_capture": rec.last_capture,
        "event": primary.kind if primary else (rec.note or "stable"),
        "note": rec.note,
    }
    if primary is not None:
        row.update({
            "severity": primary.severity(),
            "latency_bracket_s": round(primary.latency_s, 1),
            "last_live_snapshot": primary.a.raw_excerpt or None,
            "post_event_snapshot": primary.b.raw_excerpt or None,
            "detail": primary.detail,
        })
    return row


def main() -> None:
    watchlist = load_watchlist()
    if not watchlist:
        print("empty wayback watchlist — nothing to reconstruct")
        return

    kill = KillSwitch() if KillSwitch else None
    rate = RateCeiling(rate=_RATE_PER_SEC, capacity=_BURST) if RateCeiling else None
    vantage = WaybackVantagePoint(fetch_cdx=default_cdx_fetch, kill_switch=kill, rate_ceiling=rate)

    rows, ddti_observations = [], []
    reachable = 0
    for entry in watchlist:
        rec = vantage.observe(entry["url"], term=entry.get("term", ""),
                              domain=entry.get("domain", ""))
        if rec.note != "unreachable":
            reachable += 1
        rows.append(_reconstruction_row(rec))
        for d in rec.divergences:
            obs = divergence_to_observation(d)
            obs["detected_at"] = obs["detected_at"].isoformat() if hasattr(
                obs["detected_at"], "isoformat") else obs["detected_at"]
            ddti_observations.append(obs)

    # Honesty guard: if the Archive was unreachable for EVERY URL, abstain — do not publish a
    # signal that is all-unknown (it would read as "nothing is being deleted", a false zero).
    if reachable == 0:
        print("CDX unreachable for every watched URL — abstaining, not publishing a hollow signal")
        return

    now = datetime.now(timezone.utc)
    n_deletions = sum(1 for r in rows if r["event"] == DELETION)
    n_mutations = sum(1 for r in rows if r["event"] == MUTATION)
    out = {
        "generated_at": now.isoformat(),
        "source": "Internet Archive Wayback CDX API (public, outside-the-wall) x Palimpsest",
        "scope": "reconstructed deletions and silent redactions of public Chinese URLs, with "
                 "archive-witnessed timestamps; velocity reported as an explicit capture bracket",
        "n_watched": len(rows),
        "n_reachable": reachable,
        "n_deletions": n_deletions,
        "n_mutations": n_mutations,
        "reconstructions": rows,
        "ddti_observations": ddti_observations,
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "generated_at": out["generated_at"],
            "n_watched": out["n_watched"],
            "n_reachable": out["n_reachable"],
            "n_deletions": n_deletions,
            "n_mutations": n_mutations,
        }, ensure_ascii=False) + "\n")

    print(f"=== Wayback reconstruction: {len(rows)} watched, {reachable} reachable, "
          f"{n_deletions} deletions, {n_mutations} silent redactions ===")
    for r in rows:
        detail = f" bracket={r.get('latency_bracket_s')}s" if r.get("latency_bracket_s") else ""
        print(f"  {str(r['term'])[:24]:<24} {str(r['event']):<11} "
              f"captures={r['n_captures']:<4} note={r['note'] or '-'}{detail}")


if __name__ == "__main__":
    main()
