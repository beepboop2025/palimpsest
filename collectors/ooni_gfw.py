"""OONI Great-Firewall signal — live network-level censorship in China, read
from outside via OONI's public aggregation API.

OONI Probe runs on real devices inside China and uploads measurements; OONI
aggregates them into open data. We ingest that already-aggregated signal — we
never probe a censored resource ourselves. So this is **vantage-insensitive**
like the GDELT and DDTI pulls: it runs correctly from anywhere, including a
GitHub Actions runner, and never touches the box's own egress.

China caveat (load-bearing): the Great Firewall blocks via TCP-RST injection
and DNS poisoning **without serving a block page**, so OONI's `confirmed_count`
(matched a known block-page fingerprint) is near-zero for CN. The real signal
is `anomaly_count` — heuristic detection of probable interference. We track the
anomaly RATE, never confirmed.

Standard-library only (urllib + json), so it has no dependencies in CI.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

OONI_AGG = "https://api.ooni.io/api/v1/aggregation"
USER_AGENT = "palimpsest.info observatory (OONI open-data ingest; contact desk@palimpsest.info)"

# Network tests that expose the GFW from different angles: broad URL blocking,
# messenger blocking, and circumvention-tool blocking. Each is one CN query.
TESTS = [
    ("web_connectivity", "website / URL blocking"),
    ("telegram", "Telegram reachability"),
    ("whatsapp", "WhatsApp reachability"),
    ("signal", "Signal reachability"),
    ("tor", "Tor reachability"),
    ("psiphon", "Psiphon circumvention"),
    ("riseupvpn", "RiseupVPN circumvention"),
]


def _get(params: dict, timeout: float = 30.0, retries: int = 2,
         max_bytes: int = 8 * 1024 * 1024) -> dict | None:
    """One aggregation call. Fail-soft: returns None on any error (the caller
    abstains for that slice rather than publishing a false zero). Honors OONI's
    'modest request rate' with a backoff on 429. `max_bytes` caps the read —
    the per-domain breakdown is large, so callers raise it for that call; a
    truncated body fails to parse and is treated as an abstention, never a
    partial/misleading result."""
    url = f"{OONI_AGG}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read(max_bytes)
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            log.warning("OONI HTTP %s for %s", e.code, params)
            return None
        except (urllib.error.URLError, ValueError, OSError) as e:
            log.warning("OONI fetch failed for %s: %s", params, e)
            return None
    return None


def _rate(anomaly: int, measurement: int, failure: int) -> float | None:
    """Anomaly rate over measurements that actually completed. None if there is
    nothing to divide by (abstain, don't invent a 0)."""
    valid = measurement - failure
    if valid <= 0:
        return None
    return round(anomaly / valid, 4)


def test_signals(since: str, until: str, throttle: float = 1.5) -> list[dict]:
    """Per-test CN anomaly rates over [since, until). Tests that OONI couldn't
    answer are marked available=False and carry no rate (never a false zero)."""
    out = []
    for test_name, label in TESTS:
        res = _get({"probe_cc": "CN", "test_name": test_name,
                    "since": since, "until": until})
        r = (res or {}).get("result") if isinstance(res, dict) else None
        # no-axis aggregation returns a single result object
        if not isinstance(r, dict):
            out.append({"test": test_name, "label": label, "available": False})
            time.sleep(throttle)
            continue
        anomaly = int(r.get("anomaly_count") or 0)
        measurement = int(r.get("measurement_count") or 0)
        failure = int(r.get("failure_count") or 0)
        out.append({
            "test": test_name, "label": label, "available": measurement > 0,
            "anomaly_count": anomaly, "measurement_count": measurement,
            "failure_count": failure, "anomaly_rate": _rate(anomaly, measurement, failure),
        })
        time.sleep(throttle)
    return out


def top_blocked_domains(since: str, until: str, top_n: int = 25,
                        min_measurements: int = 5) -> list[dict]:
    """Which sites are most interfered-with in CN right now — from the per-domain
    breakdown of web_connectivity. Ranked by anomaly rate, with a floor on
    measurement count so a single flaky probe can't top the list."""
    # the per-domain breakdown over a week of CN web_connectivity is tens of MB
    res = _get({"probe_cc": "CN", "test_name": "web_connectivity",
                "axis_x": "domain", "since": since, "until": until},
               timeout=60.0, max_bytes=96 * 1024 * 1024)
    rows = (res or {}).get("result") if isinstance(res, dict) else None
    if not isinstance(rows, list):
        return []
    ranked = []
    for r in rows:
        domain = r.get("domain") or r.get("input")
        if not domain:
            continue
        anomaly = int(r.get("anomaly_count") or 0)
        measurement = int(r.get("measurement_count") or 0)
        failure = int(r.get("failure_count") or 0)
        if measurement < min_measurements:
            continue
        rate = _rate(anomaly, measurement, failure)
        if rate is None or rate <= 0:
            continue
        ranked.append({"domain": domain, "anomaly_count": anomaly,
                       "measurement_count": measurement, "anomaly_rate": rate})
    ranked.sort(key=lambda x: (x["anomaly_rate"], x["anomaly_count"]), reverse=True)
    return ranked[:top_n]
