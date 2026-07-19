"""IODA outage telemetry — the shutdown-scale events, seen from everywhere else.

IODA (Internet Outage Detection and Analysis, Georgia Tech) watches every
country's Internet from three independent global instruments — BGP visibility,
active probing (ping-slash24), and darknet traffic (merit-nt) — and publishes
detected outage EVENTS with per-instrument severity scores. For China this is
the heaviest end of the censorship spectrum: not a filtered domain but
connectivity itself dropping, the class of event the GFW's August 2025
unconditional port-443 block produced. IODA sees it in near-real-time with
zero in-China footprint, which is exactly this observatory's constraint.

The API is keyless JSON (https://api.ioda.inetintel.cc.gatech.edu/v2/). Two
reads per refresh:

  outages/events  — detected events: start, duration, instrument, score.
                    Multi-instrument corroboration matters (one instrument can
                    glitch); each event records WHICH instrument saw it.
  outages/summary — the window's event count + per-instrument severity medians.

The daily event COUNT feeds the conformal e-detector like every other signal;
event details are published with instrument attribution so a single-instrument
artifact is never silently promoted to a "China went dark" claim.

Standard-library only (urllib + json). Fail-soft: absence over fabrication.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

BASE = "https://api.ioda.inetintel.cc.gatech.edu/v2"
USER_AGENT = ("palimpsest.info observatory (public outage-telemetry ingest; "
              "contact desk@palimpsest.info)")
ENTITY = ("country", "CN")


def _get_json(path: str, timeout: float = 30.0) -> dict | None:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 — abstain, never fake
        log.warning("ioda %s fetch failed: %s", path, exc)
        return None


def parse_events(payload: dict) -> list[dict] | None:
    """outages/events payload -> [{start, duration_s, datasource, score}] or None.

    None distinguishes an unusable payload from a genuinely quiet window
    (which returns [] — an empty data list on a well-formed response).
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    out = []
    for e in data:
        if not isinstance(e, dict) or e.get("start") is None:
            continue
        out.append({
            "start": e["start"],
            "duration_s": e.get("duration"),
            "datasource": e.get("datasource"),
            "score": round(e["score"], 1) if isinstance(e.get("score"), (int, float)) else None,
        })
    return out


def parse_summary(payload: dict) -> dict | None:
    """outages/summary payload -> {event_cnt, scores} or None."""
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    if not data:                       # well-formed and quiet: zero events
        return {"event_cnt": 0, "scores": {}}
    row = data[0]
    scores = row.get("scores") or {}
    return {
        "event_cnt": row.get("event_cnt", 0),
        "scores": {k: round(v, 1) for k, v in scores.items()
                   if isinstance(v, (int, float))},
    }


def collect(from_ts: int, until_ts: int, fetch=_get_json) -> dict | None:
    """One window read: summary + events. None when BOTH endpoints failed."""
    etype, ecode = ENTITY
    q = f"entityType={etype}&entityCode={ecode}&from={from_ts}&until={until_ts}"
    summary = parse_summary(fetch(f"/outages/summary?{q}") or {})
    events = parse_events(fetch(f"/outages/events?{q}") or {})
    if summary is None and events is None:
        return None
    out: dict = {}
    if summary is not None:
        out["summary"] = summary
    if events is not None:
        # multi-instrument corroboration: how many distinct instruments fired
        out["events"] = events
        out["instruments_firing"] = len({e["datasource"] for e in events
                                         if e.get("datasource")})
    return out
