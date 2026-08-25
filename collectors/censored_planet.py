"""Bounded Censored Planet aggregate ingestion.

Censored Planet provides an independent public GraphQL view of DNS/HTTP
interference. Palimpsest probes nothing here: it reads three reviewed aggregate
queries from one fixed endpoint. The remote service is nevertheless treated as
hostile input, so both the HTTP capability and every returned collection are
bounded before evidence reaches publication code.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import date
from typing import Any

from core.safe_fetch import FetchError, ResponseTooLarge, safe_fetch_bytes

log = logging.getLogger(__name__)

ENDPOINT = "https://data.censoredplanet.org/query"
USER_AGENT = "palimpsest.info observatory (Censored Planet open-data ingest)"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_WINDOW_DAYS = 370
MAX_COUNTRY_ROWS = 512
MAX_EVENT_ROWS = 5_000
MAX_TIMESERIES_ROWS = 1_000

INTERFERENCE_QUERY = (
    "query($range:DateRange!){ interferenceRateByCountry(range:$range){ "
    "country unexpectedRate } }"
)
EVENTS_QUERY = (
    "query($range:DateRange!,$c:String){ cenalertEvents(range:$range, country:$c){ "
    "country startDate endDate peak impact cause reportedBy } }"
)
TIMESERIES_QUERY = (
    "query($range:DateRange!,$c:String!){ cenalertTimeseries(range:$range, country:$c){ "
    "date value } }"
)
_ALLOWED_QUERIES = frozenset({INTERFERENCE_QUERY, EVENTS_QUERY, TIMESERIES_QUERY})
_EVENT_FIELDS = {
    "country": 64,
    "startDate": 32,
    "endDate": 32,
    "cause": 4_096,
    "reportedBy": 1_024,
}


def _endpoint_policy(url: str) -> None:
    if url != ENDPOINT:
        raise FetchError("Censored Planet URL is not the reviewed endpoint")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _strict_json(raw: bytes) -> Any:
    return json.loads(
        raw.decode("utf-8", "strict"),
        object_pairs_hook=_reject_duplicates,
        parse_constant=_reject_constant,
    )


def _window(since: str, until: str) -> dict[str, str]:
    if type(since) is not str or type(until) is not str:
        raise ValueError("Censored Planet dates must be canonical text")
    try:
        start = date.fromisoformat(since)
        end = date.fromisoformat(until)
    except ValueError as exc:
        raise ValueError("Censored Planet dates must use YYYY-MM-DD") from exc
    if start.isoformat() != since or end.isoformat() != until:
        raise ValueError("Censored Planet dates must use canonical YYYY-MM-DD")
    if end < start or (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"Censored Planet window must span 0..{MAX_WINDOW_DAYS} days"
        )
    return {"startDate": since, "endDate": until}


def _finite_number(value: Any, *, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return number


def _gql(
    query: str,
    variables: dict | None = None,
    timeout: float = 30.0,
    *,
    retries: int = 2,
    sleeper=time.sleep,
    fetch_bytes=None,
):
    """Run one reviewed GraphQL query and fail soft on hostile/unavailable input."""
    if query not in _ALLOWED_QUERIES:
        raise ValueError("Censored Planet query is not in the reviewed set")
    if variables is not None and type(variables) is not dict:
        raise ValueError("Censored Planet variables must be an object")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 120
    ):
        raise ValueError("Censored Planet timeout must be in (0, 120]")
    if type(retries) is not int or not 0 <= retries <= 4:
        raise ValueError("Censored Planet retries must be in 0..4")

    body = json.dumps(
        {"query": query, "variables": variables or {}},
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("Censored Planet request exceeds its byte ceiling")

    fetch = fetch_bytes or safe_fetch_bytes
    for attempt in range(retries + 1):
        try:
            raw = fetch(
                ENDPOINT,
                method="POST",
                body=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                max_bytes=MAX_RESPONSE_BYTES,
                timeout=float(timeout),
                max_redirects=0,
                url_policy=_endpoint_policy,
            )
        except ResponseTooLarge:
            log.warning("Censored Planet response exceeded its byte ceiling")
            return None
        except FetchError:
            if attempt >= retries:
                log.warning(
                    "Censored Planet transport failed after %s attempts",
                    attempt + 1,
                )
                return None
            sleeper(2 ** attempt)
            continue

        try:
            doc = _strict_json(raw)
        except (UnicodeDecodeError, ValueError):
            log.warning("Censored Planet returned invalid JSON")
            return None
        if type(doc) is not dict:
            return None
        errors = doc.get("errors")
        if errors:
            count = len(errors) if isinstance(errors, list) else 1
            log.warning("Censored Planet returned %s GraphQL error(s)", min(count, 999))
            return None
        data = doc.get("data")
        return data if type(data) is dict else None
    return None  # pragma: no cover - bounded loop always returns


def cn_interference_rate(since: str, until: str) -> float | None:
    """China's unexpected resolution rate over a bounded date window."""
    window = _window(since, until)
    data = _gql(INTERFERENCE_QUERY, {"range": window})
    rows = (data or {}).get("interferenceRateByCountry")
    if type(rows) is not list or len(rows) > MAX_COUNTRY_ROWS:
        return None
    for row in rows:
        if type(row) is not dict:
            return None
        country = row.get("country")
        if country in ("CN", "China"):
            value = _finite_number(row.get("unexpectedRate"), low=0.0, high=100.0)
            return round(value, 2) if value is not None else None
    return None


def cn_events(since: str, until: str) -> list[dict]:
    """Return bounded, normalized China censorship-alert events."""
    window = _window(since, until)
    data = _gql(EVENTS_QUERY, {"range": window, "c": "CN"})
    rows = (data or {}).get("cenalertEvents")
    if type(rows) is not list or len(rows) > MAX_EVENT_ROWS:
        return []
    normalized: list[dict] = []
    for row in rows:
        if type(row) is not dict:
            return []
        event: dict[str, Any] = {}
        for field, maximum in _EVENT_FIELDS.items():
            value = row.get(field)
            if value is None:
                event[field] = None
            elif type(value) is str and len(value) <= maximum:
                event[field] = value
            else:
                return []
        peak = row.get("peak")
        if peak is None:
            event["peak"] = None
        else:
            number = _finite_number(peak, low=-1_000_000.0, high=1_000_000.0)
            if number is None:
                return []
            event["peak"] = number
        impact = row.get("impact")
        if impact is None:
            event["impact"] = None
        elif type(impact) is str and len(impact) <= 1_024:
            event["impact"] = impact
        else:
            number = _finite_number(
                impact, low=-1_000_000.0, high=1_000_000.0
            )
            if number is None:
                return []
            event["impact"] = number
        normalized.append(event)
    return normalized


def cn_timeseries(since: str, until: str) -> list[dict]:
    """Return a bounded, canonical China alert-intensity time series."""
    window = _window(since, until)
    start = date.fromisoformat(since)
    end = date.fromisoformat(until)
    data = _gql(TIMESERIES_QUERY, {"range": window, "c": "CN"})
    rows = (data or {}).get("cenalertTimeseries")
    if type(rows) is not list or len(rows) > MAX_TIMESERIES_ROWS:
        return []
    normalized: list[dict] = []
    for row in rows:
        if type(row) is not dict:
            return []
        raw_date = row.get("date")
        if type(raw_date) is not str:
            return []
        try:
            observed = date.fromisoformat(raw_date)
        except ValueError:
            return []
        value = _finite_number(row.get("value"), low=-1_000_000.0, high=1_000_000.0)
        if observed.isoformat() != raw_date or not start <= observed <= end or value is None:
            return []
        normalized.append({"date": raw_date, "value": value})
    return normalized
