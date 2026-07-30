"""Apple Censorship census runner — read GreatFire's App Store corpus and
publish readings/apple-censorship-latest.json.

Complements app-storefront rather than duplicating it: that signal is our own
live panel and catches a delisting the day it happens; this one is corpus scale
and, more importantly, category composition.

Vantage-insensitive, keyless, stdlib-only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.apple_censorship import (TARGET_CODE, control_state,
                                         fetch_overview, parse_country, peer_rank)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "apple-censorship-latest.json")
HIST = os.path.join(READINGS, "apple-censorship-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see, even if the numbers
# do not move. Write-if-changed compares readings, so without this a methodology
# correction that leaves the values identical never reaches the published file and
# the site keeps asserting a method it no longer uses.
METHOD_VERSION = 1



def _reading(country: dict, rank: dict | None) -> str:
    pct = country["unavailable_pct"]
    where = f", {rank['rank']} of {rank['of']} surveyed storefronts" if rank else ""
    return (f"{pct:.1f}% of {country['total_tested']:,} tested apps are unavailable in "
            f"the {country['country']} App Store{where}")


def _composition(tags: dict) -> str:
    if not tags:
        return "no category tags supplied for the blocked set this round"
    top = list(tags.items())[:4]
    return ("removals are tagged " +
            ", ".join(f"{k} {v}" for k, v in top) +
            " — the ordering is what the removals are for, not merely how many")


def main() -> None:
    now = datetime.now(timezone.utc)

    rows = fetch_overview()
    country = parse_country(rows or [], TARGET_CODE) if rows is not None else None
    control = control_state(rows, country)

    if control["state"] == "DEGRADED":
        print(f"apple-censorship: DEGRADED — {control['why']} — abstaining")
        return

    rank = peer_rank(rows, TARGET_CODE)

    out = {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "source": "AppleCensorship (api2.applecensorship.com), GreatFire's App Store corpus",
        "scope": ("corpus-scale App Store availability for the Chinese storefront and the "
                  "category composition of what is removed"),
        "relationship": ("complements the app-storefront signal, which is our own live "
                         "panel and detects individual delisting events; this is scale "
                         "and composition, and does not feed that signal's rate"),
        "control": control,
        "country": country,
        "unavailable_pct": country["unavailable_pct"],
        "reading": _reading(country, rank),
        "composition_reading": _composition(country["tags"]),
        "peer_rank": rank,
    }

    prev = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, json.JSONDecodeError):
            prev = {}

    changed = (prev.get("unavailable_pct") != out["unavailable_pct"]
               or (prev.get("country") or {}).get("unavailable") != country["unavailable"]
               or (prev.get("country") or {}).get("tags") != country["tags"])
    # A method correction must reach the published file even when every value is
    # identical, or the site keeps asserting a method it no longer uses.
    changed = changed or prev.get("method_version") != METHOD_VERSION

    os.makedirs(READINGS, exist_ok=True)
    if changed or not prev:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "unavailable_pct": country["unavailable_pct"],
                "unavailable": country["unavailable"],
                "total_tested": country["total_tested"],
                "deletions": country["deletions"],
                "tags": country["tags"],
            }, ensure_ascii=False) + "\n")
        print(f"apple-censorship: {country['unavailable_pct']}% unavailable "
              f"({country['unavailable']:,}/{country['total_tested']:,}), "
              f"tags={list(country['tags'])[:4]}")
    else:
        print(f"apple-censorship: unchanged ({country['unavailable_pct']}%) — not rewriting")


if __name__ == "__main__":
    main()
