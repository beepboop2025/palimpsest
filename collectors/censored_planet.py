"""Censored Planet signal — independent DNS/HTTP censorship measurement for
China, via the open GraphQL API (data.censoredplanet.org/query).

Censored Planet (U. Michigan) measures interference from 95k+ vantage points
using remote side-channel techniques (Satellite/DNS, Hyperquack/HTTP-SNI) —
a *different method* from OONI's active probes. Ingesting it gives Palimpsest
methodological triangulation: two independent measurement approaches agreeing
on what China blocks is far stronger than one. Vantage-insensitive (we query
their public aggregated API; we probe nothing), keyless, stdlib-only.

Schema (introspected 2026-07): interferenceRateByCountry{country,unexpectedRate},
cenalertEvents{country,startDate,endDate,peak,impact,cause,reportedBy},
cenalertTimeseries{value,date,country}. DateRange = {startDate,endDate}.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

ENDPOINT = "https://data.censoredplanet.org/query"
USER_AGENT = "palimpsest.info observatory (Censored Planet open-data ingest)"


def _gql(query: str, variables: dict | None = None, timeout: float = 30.0):
    """One GraphQL call. Fail-soft: returns None on transport error or GraphQL
    error, so the caller abstains rather than publishing a false zero."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(ENDPOINT, data=body,
                                 headers={"content-type": "application/json",
                                          "User-Agent": USER_AGENT})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read(16 * 1024 * 1024)
        doc = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as e:
        log.warning("Censored Planet fetch failed: %s", e)
        return None
    if doc.get("errors"):
        log.warning("Censored Planet GraphQL error: %s", doc["errors"])
        return None
    return doc.get("data")


def cn_interference_rate(since: str, until: str) -> float | None:
    """China's 'unexpected' (interfered) resolution rate over the window — CP's
    headline interference measure, in percent. None if CN isn't in the result."""
    d = _gql(
        "query($range:DateRange!){ interferenceRateByCountry(range:$range){ country unexpectedRate } }",
        {"range": {"startDate": since, "endDate": until}})
    rows = (d or {}).get("interferenceRateByCountry") or []
    for r in rows:
        if r.get("country") in ("CN", "China"):
            v = r.get("unexpectedRate")
            return round(float(v), 2) if v is not None else None
    return None


def cn_events(since: str, until: str) -> list[dict]:
    """Discrete China censorship-alert events in the window (start/end, peak,
    impact, cause, who reported). Empty list if none / unavailable."""
    d = _gql(
        "query($range:DateRange!,$c:String){ cenalertEvents(range:$range, country:$c){ "
        "country startDate endDate peak impact cause reportedBy } }",
        {"range": {"startDate": since, "endDate": until}, "c": "CN"})
    ev = (d or {}).get("cenalertEvents") or []
    return ev if isinstance(ev, list) else []


def cn_timeseries(since: str, until: str) -> list[dict]:
    """China alert-intensity time series {date, value} for charting/history."""
    d = _gql(
        "query($range:DateRange!,$c:String!){ cenalertTimeseries(range:$range, country:$c){ "
        "date value } }",
        {"range": {"startDate": since, "endDate": until}, "c": "CN"})
    ts = (d or {}).get("cenalertTimeseries") or []
    return ts if isinstance(ts, list) else []
