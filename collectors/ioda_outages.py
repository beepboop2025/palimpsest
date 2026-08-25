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

Standard-library only (shared safe transport + json). Fail-soft: absence over fabrication.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

BASE = "https://api.ioda.inetintel.cc.gatech.edu/v2"
USER_AGENT = ("palimpsest.info observatory (public outage-telemetry ingest; "
              "contact desk@palimpsest.info)")
ENTITY = ("country", "CN")
MAX_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 10_000
MAX_WINDOW_SECONDS = 31 * 24 * 60 * 60
_PATH = re.compile(
    r"/outages/(?:summary|events)\?entityType=country&entityCode=CN&from=\d+&until=\d+\Z"
)


def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _get_json(
    path: str,
    timeout: float = 30.0,
    *,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> dict | None:
    if type(path) is not str or _PATH.fullmatch(path) is None:
        log.warning("ioda refused an invalid API path")
        return None
    url = BASE + path

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("IODA API URL changed")

    try:
        payload = fetcher(
            url,
            timeout=timeout,
            max_bytes=MAX_BYTES,
            max_redirects=0,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            url_policy=exact_url,
        )
        if len(payload) > MAX_BYTES:
            raise FetchError("IODA response exceeded its byte budget")
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
        return document if isinstance(document, dict) else None
    except Exception as exc:  # noqa: BLE001 — abstain, never fake
        log.warning("ioda fetch failed (%s)", type(exc).__name__)
        return None


def parse_events(payload: dict) -> list[dict] | None:
    """outages/events payload -> [{start, duration_s, datasource, score}] or None.

    None distinguishes an unusable payload from a genuinely quiet window
    (which returns [] — an empty data list on a well-formed response).
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or len(data) > MAX_EVENTS:
        return None
    out = []
    for e in data:
        if not isinstance(e, dict):
            continue
        start = e.get("start")
        duration = e.get("duration")
        datasource = e.get("datasource")
        score = e.get("score")
        if (
            type(start) not in {int, float}
            or not math.isfinite(start)
            or start < 0
            or (
                duration is not None
                and (
                    type(duration) not in {int, float}
                    or not math.isfinite(duration)
                    or duration < 0
                )
            )
            or (datasource is not None and (
                not isinstance(datasource, str) or len(datasource) > 128
            ))
            or (score is not None and (
                type(score) not in {int, float} or not math.isfinite(score)
            ))
        ):
            continue
        out.append({
            "start": start,
            "duration_s": duration,
            "datasource": datasource,
            "score": round(score, 1) if score is not None else None,
        })
    return out


def parse_summary(payload: dict) -> dict | None:
    """outages/summary payload -> {event_cnt, scores} or None."""
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or len(data) > 16:
        return None
    if not data:                       # well-formed and quiet: zero events
        return {"event_cnt": 0, "scores": {}}
    row = data[0]
    if not isinstance(row, dict):
        return None
    scores = row.get("scores") or {}
    event_count = row.get("event_cnt", 0)
    if (
        not isinstance(scores, dict)
        or len(scores) > 32
        or type(event_count) is not int
        or not 0 <= event_count <= MAX_EVENTS
    ):
        return None
    return {
        "event_cnt": event_count,
        "scores": {k: round(v, 1) for k, v in scores.items()
                   if isinstance(k, str) and len(k) <= 128
                   and type(v) in {int, float} and math.isfinite(v)},
    }


def collect(from_ts: int, until_ts: int, fetch=_get_json) -> dict | None:
    """One window read: summary + events. None when BOTH endpoints failed."""
    if (
        type(from_ts) is not int
        or type(until_ts) is not int
        or from_ts < 0
        or until_ts <= from_ts
        or until_ts - from_ts > MAX_WINDOW_SECONDS
    ):
        log.warning("ioda refused an invalid collection window")
        return None
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
