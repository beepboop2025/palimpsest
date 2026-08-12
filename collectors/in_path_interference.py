"""In-Path Interference — direct evidence of middleboxes tampering with traffic
inside China, plus the tests that cannot run there at all.

This is deliberately NOT an extension of collectors/ooni_gfw.py. That signal
tracks reachability (can you get to a site, a messenger, a circumvention tool)
and publishes a measurement-weighted index whose history only means something if
its test set stays fixed. Adding tests to it would silently redefine a published
number. So this is a separate reading of a separate phenomenon.

WHAT THIS MEASURES

Three families, all from OONI's public aggregation API for probe_cc=CN:

  * MIDDLEBOX  — http_header_field_manipulation and http_invalid_request_line.
    These do not ask "is the site blocked". They send deliberately malformed or
    oddly-cased HTTP and check whether anything on the path rewrote it. A
    non-zero rate is positive evidence of a device in the middle, independent of
    whether it chose to block anything.

  * TRANSPORT  — stunreachability, vanilla_tor, torsf. Whether the plumbing that
    real-time voice and pluggable-transport circumvention depend on is usable.

  * EXECUTION  — dnscheck and echcheck, which in CN return failure on essentially
    every run.

THE DISTINCTION THAT MATTERS

OONI reports measurement_count, anomaly_count, failure_count and ok_count. An
anomaly is a COMPLETED test whose result looked like interference. A failure is a
test that never produced a result at all. They are not interchangeable, and
conflating them is the easiest way to publish a wrong number here:

    dnscheck, CN, 90 days: 71,259 measurements, 71,259 failures, 0 anomalies.

That is not "100% censored". The anomaly counter ranges only over completed runs,
so a test that never completes has no anomaly rate to report. What it has is an
execution-failure rate, which is a real and interesting observation of its own
kind: an entire measurement method that cannot be carried out inside the country.
So rates are computed over COMPLETED tests only (measurement_count - failure_count),
and execution failure is carried in its own field where it cannot be mistaken for
interference.

VANTAGE: vantage-insensitive. We ingest OONI's already-aggregated open data and
probe nothing ourselves, so this runs correctly from a GitHub runner and never
touches the box's egress. Keyless, standard-library only.
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
USER_AGENT = ("palimpsest.info observatory (OONI open-data ingest; "
              "contact desk@palimpsest.info)")

# A family with too few measurements cannot support a rate. openvpn returned 6
# measurements for CN over 90 days, which is noise, so anything under this floor
# is reported as present-but-insufficient rather than given a percentage.
MIN_COMPLETED = 100

TESTS = [
    ("http_header_field_manipulation", "MIDDLEBOX",
     "HTTP headers rewritten in transit"),
    ("http_invalid_request_line", "MIDDLEBOX",
     "malformed request lines normalised in transit"),
    ("stunreachability", "TRANSPORT",
     "STUN servers unreachable (real-time voice / WebRTC)"),
    ("vanilla_tor", "TRANSPORT", "stock Tor bootstrap"),
    ("torsf", "TRANSPORT", "Tor via Snowflake"),
    ("dnscheck", "EXECUTION", "DNS-integrity test completion"),
    ("echcheck", "EXECUTION", "Encrypted Client Hello test completion"),
]


def _get(params: dict, *, timeout: float = 30.0, retries: int = 2,
         opener=None) -> dict | None:
    """One aggregation query. Returns None rather than raising so a single dead
    test does not take the whole round down; the caller counts the Nones."""
    url = f"{OONI_AGG}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    _open = opener or urllib.request.urlopen
    for attempt in range(retries + 1):
        try:
            with _open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2 ** attempt)          # politeness, not decoration
                continue
            log.warning("OONI %s failed: %s", params.get("test_name"), e.code)
            return None
        except Exception as e:                    # noqa: BLE001
            log.warning("OONI %s failed: %s", params.get("test_name"), e)
            return None
    return None


def observe_test(test_name: str, family: str, label: str, since: str, until: str,
                 *, get=_get) -> dict:
    """Aggregate one OONI test for CN over the window."""
    doc = get({"probe_cc": "CN", "test_name": test_name,
               "since": since, "until": until})
    if doc is None:
        return {"test": test_name, "family": family, "label": label,
                "available": False, "why": "OONI did not answer"}

    r = doc.get("result") or {}
    if isinstance(r, list):
        r = r[0] if r else {}

    total = r.get("measurement_count") or 0
    failed = r.get("failure_count") or 0
    anomalies = r.get("anomaly_count") or 0
    completed = total - failed

    # Rates are over COMPLETED tests. A test that never completes has no anomaly
    # rate, only an execution-failure rate, and the two must not be confused.
    anomaly_rate = round(anomalies / completed, 4) if completed >= MIN_COMPLETED else None
    execution_failure_rate = round(failed / total, 4) if total > 0 else None

    if total == 0:
        available, why = False, "no CN measurements in the window"
    elif completed < MIN_COMPLETED:
        available, why = False, (f"only {completed} completed measurement(s); "
                                 f"below the {MIN_COMPLETED} floor for a rate")
    else:
        available, why = True, None

    return {
        "test": test_name,
        "family": family,
        "label": label,
        "available": available,
        "why": why,
        "measurement_count": total,
        "completed_count": completed,
        "failure_count": failed,
        "anomaly_count": anomalies,
        "anomaly_rate": anomaly_rate,
        "execution_failure_rate": execution_failure_rate,
        "never_completes": bool(total > 0 and completed == 0),
    }


def observe_all(since: str, until: str, *, get=_get) -> list:
    return [observe_test(t, fam, label, since, until, get=get)
            for t, fam, label in TESTS]


def family_completed_count(observations: list, family: str) -> int:
    """Return the exact completed-test denominator used by ``family_index``."""
    return sum(
        o["completed_count"]
        for o in observations
        if o["family"] == family and o.get("available")
    )


def family_index(observations: list, family: str) -> float | None:
    """Measurement-weighted anomaly rate across the available tests in one
    family, 0-100. Weighting by completed count stops a thinly-measured test
    from dominating a family."""
    usable = [o for o in observations
              if o["family"] == family and o.get("available")]
    denom = family_completed_count(observations, family)
    if not denom:
        return None
    num = sum(o["anomaly_count"] for o in usable)
    return round(100 * num / denom, 2)


def execution_blackouts(observations: list) -> list:
    """Tests that ran in CN and never once completed. Not an interference rate;
    a statement about what cannot be measured there at all."""
    return [{"test": o["test"], "label": o["label"],
             "measurement_count": o["measurement_count"]}
            for o in observations if o.get("never_completes")]


def control_state(observations: list) -> dict:
    """A round is trustworthy when OONI answered for at least one test AND at
    least one family can support a rate. If OONI is unreachable or rate-limiting,
    every test reads unavailable, which would otherwise look like a country that
    went quiet."""
    # "reachable" = OONI returned a document we could read counts from, whether
    # or not those counts cleared the floor.
    reachable = [o for o in observations if o.get("measurement_count") is not None]
    usable = [o for o in observations if o.get("available")]

    if not reachable:
        return {"state": "DEGRADED",
                "why": "OONI answered for no test at all (unreachable or rate-limited); "
                       "this is an API failure, not an observation about China"}
    if not usable:
        return {"state": "DEGRADED",
                "why": "OONI answered but no test cleared the measurement floor; "
                       "no rate can be supported this round"}
    return {"state": "OK",
            "why": f"{len(usable)} of {len(observations)} tests have enough completed "
                   f"measurements to support a rate"}
