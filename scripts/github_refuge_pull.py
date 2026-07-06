"""GitHub-as-Refuge runner — watch the pressure on censored mirrors from outside the wall.

GitHub is where censored material in China takes refuge (996.ICU, nCovMemory, the
Terminus2049 archive). This runner watches a human-curated, evidence-bound list of
those refuge repos plus GitHub's own transparency repos, and records PUBLIC pressure
metadata only: a repo going 451 (GitHub's documented legal-takedown status), a
previously-present repo returning 404 (taken down), archived/disabled visibility
drops, and defensive fork/star bursts (the preservation reflex). It never touches the
censored content itself and never collects anything about a maintainer.

Anti-false-positive discipline (built into the collector): a 404 is only a takedown
when a prior PRESENT (200) baseline exists for that repo. A first-contact 404 — a
renamed or owner-deleted repo — abstains, never fabricates a takedown. That is why
this runner PERSISTS a baseline store (readings/github-refuge-baselines.json) across
runs: the first run only records presence, and pressure events fall out on later runs.

Vantage note: GitHub's API is read from outside the wall, so this is
vantage-insensitive — it measures pressure ON the refuge, from a safe vantage.
Standard-library only. Read-only: never writes to GitHub.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.github_refuge import GitHubRefugeCollector, github_fetch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
WATCHLIST = os.path.join(ROOT, "config", "github_refuge_watchlist.json")
BASELINES = os.path.join(READINGS, "github-refuge-baselines.json")
OUT = os.path.join(READINGS, "github-refuge-latest.json")
HIST = os.path.join(READINGS, "github-refuge-history.jsonl")


class FileBaselineStore:
    """Persisted per-repo baseline store: get/put backed by one JSON file that is
    committed to the repo, so presence/fork/star history survives across Action runs.
    This persistence is what lets a later 404 be told apart from a repo we never saw."""

    def __init__(self, path: str):
        self.path = path
        self._d = {}
        if os.path.exists(path):
            try:
                self._d = json.load(open(path, encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._d = {}

    def get(self, key):
        return self._d.get(key)

    def put(self, key, value):
        self._d[key] = value

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._d, f, ensure_ascii=False, indent=2, sort_keys=True)


def load_active_watchlist() -> list:
    """Activate the evidence-bound documented_repos + GitHub's transparency repos.
    These are public, well-documented refuge/transparency repos — safe to watch."""
    doc = json.load(open(WATCHLIST, encoding="utf-8"))
    active = list(doc.get("active_watchlist") or [])
    active += doc.get("documented_repos", [])
    for t in doc.get("_meta", {}).get("transparency_repos", []):
        active.append({"full_name": t["full_name"], "terms": ["transparency", "dmca"]})
    # de-dup by full_name, keep first
    seen, out = set(), []
    for e in active:
        fn = e.get("full_name")
        if fn and fn not in seen:
            seen.add(fn)
            out.append(e)
    return out


def main() -> None:
    watchlist = load_active_watchlist()
    if not watchlist:
        print("empty watchlist — nothing to scan")
        return

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")  # raises 60->5000/hr; correctness never needs it

    def fetch(url):
        return github_fetch(url, token=token)

    store = FileBaselineStore(BASELINES)
    first_run = not os.path.exists(BASELINES)

    collector = GitHubRefugeCollector(
        {"watchlist": watchlist},
        fetch=fetch,
        baseline_store=store,
    )
    result = collector.scan()
    store.save()  # persist presence/counts for next run's takedown + burst detection

    readings = result.get("readings", [])
    now = datetime.now(timezone.utc)
    # Pressure events = non-abstained, informative readings (takedown / legal_block /
    # visibility_down / preservation burst). "present, nothing happening" is honest, not noise.
    pressure = [r for r in readings if not r.get("abstained") and r.get("kind") not in ("quiet", "abstain")]

    out = {
        "generated_at": now.isoformat(),
        "source": "GitHub REST API (public pressure metadata only)",
        "scope": "pressure on censored-material refuge repos: takedowns, legal blocks, visibility drops, preservation bursts",
        "first_run": first_run,
        "n_watched": len(readings),
        "n_present": sum(1 for r in readings if r.get("status") == "present"),
        "n_pressure_events": len(pressure),
        "reachability": result.get("reachability", {}),
        "watched": [
            {
                "full_name": r.get("full_name"),
                "status": r.get("status"),
                "pressure_likelihood": r.get("pressure_likelihood"),
                "kind": r.get("kind"),
                "severity": r.get("severity"),
                "fork_novelty": r.get("fork_novelty"),
                "star_novelty": r.get("star_novelty"),
                "dmca": r.get("dmca", []),
                "detail": r.get("detail", ""),
                "topic_terms": r.get("topic_terms", []),
            }
            for r in readings
        ],
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "generated_at": out["generated_at"],
            "n_watched": out["n_watched"],
            "n_present": out["n_present"],
            "n_pressure_events": out["n_pressure_events"],
            "first_run": first_run,
        }, ensure_ascii=False) + "\n")

    tag = " (FIRST RUN — recording baselines, pressure events start next cycle)" if first_run else ""
    print(f"=== GitHub-refuge: {out['n_watched']} watched, {out['n_present']} present, "
          f"{out['n_pressure_events']} pressure events{tag} ===")
    for r in readings:
        print(f"  {str(r.get('full_name'))[:32]:<32} {str(r.get('status')):<15} "
              f"sev={r.get('severity')} pl={r.get('pressure_likelihood')}")


if __name__ == "__main__":
    main()
