"""net4people/bbs event signal runner — publish readings/net4people-latest.json:
the recent China firewall / circumvention events, an event-velocity read, and
the latest items. Vantage-insensitive, key-less-capable, stdlib-only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from collectors.net4people_events import fetch_issues, normalize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "net4people-latest.json")
HIST = os.path.join(READINGS, "net4people-history.jsonl")

RECENT_DAYS = 30
SHOW = 20


def _age_days(iso: str, now: datetime) -> float | None:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (now - t).total_seconds() / 86400
    except (ValueError, AttributeError):
        return None


def main() -> None:
    issues = fetch_issues()
    if not issues:
        print("net4people/bbs returned no issues (unreachable / rate-limited) — abstaining")
        return

    now = datetime.now(timezone.utc)
    items = [normalize(i) for i in issues if i.get("title")]
    # newest first is already the API order; keep it explicit
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    recent = [x for x in items if (_age_days(x.get("created_at"), now) or 999) <= RECENT_DAYS]
    n_recent = len(recent)
    n_block = sum(1 for x in recent if x["kind"] == "blocking")
    n_circ = sum(1 for x in recent if x["kind"] == "circumvention")

    # event velocity: recent-window rate vs the trailing baseline (issues/day
    # over the whole pulled set). >1 means blocking/circumvention chatter is
    # running hotter than usual — a soft "arms race heating up" read.
    span = _age_days(items[-1].get("created_at"), now) if items else None
    baseline_rate = (len(items) / span) if span and span > 0 else None
    recent_rate = n_recent / RECENT_DAYS
    velocity = round(recent_rate / baseline_rate, 2) if baseline_rate else None

    out = {
        "generated_at": now.isoformat(),
        "source": "net4people/bbs GitHub issues (github.com/net4people/bbs)",
        "scope": ("community log of China network-blocking events and circumvention "
                  "developments — the qualitative companion to the OONI anomaly signal"),
        "method": "ingests a public overseas GitHub repo's issues; probes nothing (vantage-insensitive)",
        "recent_days": RECENT_DAYS,
        "n_recent": n_recent,
        "n_blocking": n_block,
        "n_circumvention": n_circ,
        "velocity": velocity,
        "velocity_reading": (
            "no baseline yet" if velocity is None else
            f"{velocity}x normal — chatter elevated" if velocity >= 1.4 else
            f"{velocity}x normal — quiet" if velocity <= 0.6 else
            f"{velocity}x normal — typical"),
        "events": [
            {k: x[k] for k in ("number", "title", "url", "created_at", "labels",
                               "kind", "china_tagged", "comments")}
            for x in items[:SHOW]
        ],
    }
    os.makedirs(READINGS, exist_ok=True)

    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
    # change = a new top issue number or a changed recent count
    changed = (not prev or
               (prev.get("events") or [{}])[0].get("number") != (out["events"] or [{}])[0].get("number") or
               prev.get("n_recent") != out["n_recent"])
    if changed:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "n_recent": n_recent, "n_blocking": n_block,
                "n_circumvention": n_circ, "velocity": velocity,
                "top_number": (out["events"] or [{}])[0].get("number"),
            }, ensure_ascii=False) + "\n")

    print(f"=== net4people/bbs — {n_recent} events/{RECENT_DAYS}d "
          f"({n_block} blocking, {n_circ} circumvention); velocity {velocity} ===")
    for x in items[:8]:
        tag = "CN" if x["china_tagged"] else "  "
        print(f"  [{tag}] {x['kind']:<13} #{x['number']:<6} {x['title'][:58]}")


if __name__ == "__main__":
    main()
