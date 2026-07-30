"""App Storefront runner — read Apple's public catalogue for the CN and US
storefronts and publish readings/app-storefront-latest.json.

Vantage-insensitive, key-less, standard-library only. Follows the house pattern:
collect -> control gate -> honesty guard / abstain -> write-if-changed -> history.

The history file is the point of this signal. A delisting is an event: it happens
on a date, to a named app, and it is the moment a company was compelled to act.
The latest reading tells you the current state of the catalogue; the history is
the record of when each door closed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.app_storefront import (PANEL, control_state, delisting_rate,
                                       observe_panel)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "app-storefront-latest.json")
HIST = os.path.join(READINGS, "app-storefront-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see, even if the numbers
# do not move. Write-if-changed compares readings, so without this a methodology
# correction that leaves the values identical never reaches the published file and
# the site keeps asserting a method it no longer uses.
METHOD_VERSION = 1



def _band(rate: float) -> str:
    if rate >= 0.75:
        return "most of the tracked catalogue is unavailable in the Chinese storefront"
    if rate >= 0.4:
        return "much of the tracked catalogue is unavailable in the Chinese storefront"
    if rate > 0:
        return "part of the tracked catalogue is unavailable in the Chinese storefront"
    return "every tracked app is currently offered in the Chinese storefront"


def _diff(prev_states: dict, observations: list) -> list:
    """Name what changed since the previous reading. A transition is the event
    this signal exists to catch, so it is recorded explicitly rather than left
    for a reader to infer by diffing two files."""
    events = []
    for o in observations:
        was = prev_states.get(str(o["track_id"]))
        now = o["state"]
        if was and was != now and {was, now} <= {"DELISTED", "AVAILABLE"}:
            events.append({
                "app": o["name"],
                "track_id": o["track_id"],
                "category": o["category"],
                "from": was,
                "to": now,
                "direction": "delisted" if now == "DELISTED" else "relisted",
            })
    return events


def main() -> None:
    now = datetime.now(timezone.utc)

    observations = observe_panel()
    control = control_state(observations)

    if control["state"] == "DEGRADED":
        print(f"app-storefront: DEGRADED — {control['why']} — abstaining, not publishing")
        return

    rate = delisting_rate(observations)
    if rate is None:
        print("app-storefront: no comparable app — abstaining")
        return

    prev, prev_states = {}, {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                prev = json.load(f)
            prev_states = {str(a["track_id"]): a["state"] for a in prev.get("apps", [])}
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            prev, prev_states = {}, {}

    events = _diff(prev_states, observations)
    tracked = [o for o in observations if not o["control"]]
    delisted = [o for o in tracked if o["state"] == "DELISTED"]

    out = {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "source": "Apple iTunes lookup API (itunes.apple.com), storefronts cn vs us",
        "scope": ("app-layer availability: which apps are offered in the Chinese App "
                  "Store storefront versus the US one"),
        "caveat": ("delisting is NOT network blocking. Facebook, X and YouTube remain "
                   "listed in the Chinese storefront while unreachable there; the apps "
                   "that get removed are the ones that would still work over a VPN"),
        "control": control,
        "delisting_rate": rate,
        "reading": _band(rate),
        "n_tracked": len(tracked),
        "n_delisted": len(delisted),
        "delisted_apps": [o["name"] for o in delisted],
        "changes_since_last_reading": events,
        "panel_size": len(PANEL),
        "apps": observations,
    }

    changed = (prev.get("delisting_rate") != rate
               or {a["name"] for a in prev.get("apps", []) if a.get("state") == "DELISTED"}
               != {o["name"] for o in delisted}
               or bool(events))
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
                "delisting_rate": rate,
                "n_delisted": len(delisted),
                "delisted_apps": out["delisted_apps"],
                "changes": events,
            }, ensure_ascii=False) + "\n")
        msg = f"app-storefront: rate={rate} ({len(delisted)}/{len(tracked)} delisted)"
        if events:
            msg += " | " + "; ".join(f"{e['app']} {e['direction']}" for e in events)
        print(msg)
    else:
        print(f"app-storefront: unchanged (rate={rate}) — not rewriting")


if __name__ == "__main__":
    main()
