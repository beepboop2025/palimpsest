"""China economic telemetry — the state's own money-market benchmarks, read
from the CFETS chinamoney English portal.

Palimpsest reads what the Chinese state publishes, hides, and deletes. This
collector reads what it PUBLISHES on the financial side: the interbank
benchmarks CFETS/NIFC posts daily (SHIBOR fixings, pledged-repo fixing rates,
the USD/CNY central parity fix). These are official state-published numbers —
the latent-state read here is not censorship but policy: FDR007 vs the 7-day
OMO rate is where the PBOC's true stance shows before any announcement, and
the parity fix is where FX policy shows daily.

Vantage notes (probed live 2026-07-13): the portal is keyless and serves
international traffic, but it is range-limited (~1 month of history per
request) and burst-throttles — rapid consecutive hits return EMPTY bodies,
not errors. So this collector makes exactly three requests per run, spaced
well apart. It hashes the exact response bytes before parsing; the publisher
keeps a latest-per-date compatibility history plus an append-only bitemporal
revision ledger, so the archive outgrows the API's window without erasing
revisions.

Standard-library only (shared safe transport + json), no dependencies in CI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as calendar_date
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

BASE = "https://www.chinamoney.com.cn"
USER_AGENT = "palimpsest.info observatory (official-benchmark ingest; contact desk@palimpsest.info)"
SPACING_S = 20.0   # between the three calls — the portal punishes bursts
MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 128
MAX_WINDOW_DAYS = 45

SHIBOR_TENORS = ["ON", "1W", "2W", "1M", "3M", "6M", "9M", "1Y"]
FRR_KEYS = ["FR001", "FR007", "FR014", "FDR001", "FDR007", "FDR014"]


@dataclass(frozen=True, slots=True)
class PortalResponse:
    """Parsed payload plus the identity of the exact bytes and request URL."""

    data: Mapping[str, object]
    raw_sha256: str
    evidence_url: str


@dataclass(frozen=True, slots=True)
class FamilyCollection:
    """One benchmark family's values and response-level provenance."""

    values: Mapping[str, Mapping[str, float]]
    raw_sha256: str | None
    evidence_url: str


@dataclass(frozen=True, slots=True)
class ChinaEconCollection:
    """Merged values with independent provenance for every responding family."""

    values: Mapping[str, Mapping[str, float]]
    provenance: Mapping[str, FamilyCollection]


def _get(
    path: str,
    referer: str,
    timeout: float = 30.0,
    retries: int = 2,
    *,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> PortalResponse | None:
    """One portal call. Fail-soft: None on any error, and an EMPTY body counts
    as an error (that is the throttle speaking, not a data statement)."""
    url = f"{BASE}{path}"

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("ChinaMoney request URL changed")

    for attempt in range(retries + 1):
        try:
            _validate_portal_request(path, referer)
            raw = fetcher(
                url,
                timeout=timeout,
                max_bytes=MAX_BYTES,
                max_redirects=0,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Referer": referer,
                    "User-Agent": USER_AGENT,
                },
                url_policy=exact_url,
            )
            if len(raw) > MAX_BYTES:
                raise FetchError("ChinaMoney response exceeded its byte budget")
            if not raw.strip():
                raise ValueError("empty body (throttled)")
            decoded = raw.decode("utf-8")
            parsed = json.loads(
                decoded,
                object_pairs_hook=_reject_duplicates,
                parse_constant=_reject_constant,
            )
            if not isinstance(parsed, dict):
                raise ValueError("top-level response must be an object")
            return PortalResponse(
                data=parsed,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                evidence_url=url,
            )
        except Exception as exc:  # noqa: BLE001 — abstain, never fake
            log.warning(
                "chinamoney request attempt %d failed (%s)",
                attempt,
                type(exc).__name__,
            )
            if attempt < retries:
                time.sleep(30.0 * (attempt + 1))
    return None


def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _validate_portal_request(path: str, referer: str) -> None:
    if type(path) is not str or not path.startswith("/") or len(path) > 2048:
        raise FetchError("ChinaMoney path is invalid")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment:
        raise FetchError("ChinaMoney path must be relative and fragment-free")
    allowed = {
        "/ags/ms/cm-u-bk-shibor/ShiborHis": (
            {"lang", "startDate", "endDate"},
            f"{BASE}/english/bmkshibor/",
        ),
        "/ags/ms/cm-u-bk-currency/FrrHis": (
            {"lang", "startDate", "endDate"},
            f"{BASE}/english/bmkfrr/",
        ),
        "/ags/ms/cm-u-bk-ccpr/CcprHisNew": (
            {"startDate", "endDate", "currency"},
            f"{BASE}/english/bmkcpr/",
        ),
    }
    policy = allowed.get(parts.path)
    if policy is None or referer != policy[1]:
        raise FetchError("ChinaMoney endpoint or referer is not reviewed")
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    if len(pairs) != len(policy[0]) or {key for key, _value in pairs} != policy[0]:
        raise FetchError("ChinaMoney query shape is not reviewed")
    query = dict(pairs)
    if query.get("lang", "EN").upper() != "EN":
        raise FetchError("ChinaMoney language is not reviewed")
    if "currency" in query and query["currency"] != "USD/CNY":
        raise FetchError("ChinaMoney currency is not reviewed")
    _validated_window(query.get("startDate"), query.get("endDate"))


def _validated_window(
    start: object,
    end: object,
) -> tuple[calendar_date, calendar_date]:
    if type(start) is not str or type(end) is not str:
        raise ValueError("ChinaMoney dates must be text")
    try:
        first = calendar_date.fromisoformat(start)
        last = calendar_date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("ChinaMoney dates must be ISO calendar dates") from exc
    if first.isoformat() != start or last.isoformat() != end:
        raise ValueError("ChinaMoney dates must be canonical ISO dates")
    if first > last or (last - first).days > MAX_WINDOW_DAYS:
        raise ValueError("ChinaMoney date window is invalid or too wide")
    return first, last


def _records(response: PortalResponse | None) -> list[dict]:
    if response is None:
        return []
    records = response.data.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        return []
    return [record for record in records if isinstance(record, dict)]


def _record_date(
    value: object,
    first: calendar_date,
    last: calendar_date,
) -> str | None:
    if type(value) is not str or len(value) < 10:
        return None
    candidate = value[:10]
    try:
        parsed = calendar_date.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.isoformat() != candidate or not first <= parsed <= last:
        return None
    return candidate


def _num(v, *, ceiling: float) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        value = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or not 0 <= value <= ceiling:
        return None
    return value


def fetch_shibor(start: str, end: str) -> FamilyCollection:
    """date -> {shibor_on: x, shibor_1w: x, ...}. Empty dict on failure."""
    first, last = _validated_window(start, end)
    path = f"/ags/ms/cm-u-bk-shibor/ShiborHis?lang=en&startDate={start}&endDate={end}"
    response = _get(
        path,
        referer=f"{BASE}/english/bmkshibor/",
    )
    out: dict[str, dict[str, float]] = {}
    for rec in _records(response):
        record_date = _record_date(rec.get("showDateCN"), first, last)
        if record_date is None:
            continue
        row = {}
        for tenor in SHIBOR_TENORS:
            v = _num(rec.get(tenor), ceiling=100.0)
            if v is not None:
                row[f"shibor_{tenor.lower()}"] = v
        if row:
            out[record_date] = row
    return FamilyCollection(
        values=out,
        raw_sha256=response.raw_sha256 if response else None,
        evidence_url=response.evidence_url if response else f"{BASE}{path}",
    )


def fetch_repo_fixings(start: str, end: str) -> FamilyCollection:
    """date -> {fr001: x, fdr007: x, ...}. FDR = depository-institutions repo
    fixing, the closest public daily proxy to the DR007 policy anchor."""
    first, last = _validated_window(start, end)
    path = f"/ags/ms/cm-u-bk-currency/FrrHis?lang=EN&startDate={start}&endDate={end}"
    response = _get(
        path,
        referer=f"{BASE}/english/bmkfrr/",
    )
    out: dict[str, dict[str, float]] = {}
    for rec in _records(response):
        record_date = _record_date(rec.get("lfiProducDate"), first, last)
        vals = rec.get("frValueMap") or {}
        if record_date is None or not isinstance(vals, dict):
            continue
        row = {}
        for key in FRR_KEYS:
            v = _num(vals.get(key), ceiling=100.0)
            if v is not None:
                row[key.lower()] = v
        if row:
            out[record_date] = row
    return FamilyCollection(
        values=out,
        raw_sha256=response.raw_sha256 if response else None,
        evidence_url=response.evidence_url if response else f"{BASE}{path}",
    )


def fetch_parity(start: str, end: str) -> FamilyCollection:
    """date -> {usdcny_parity: x} — the daily central parity fix."""
    first, last = _validated_window(start, end)
    path = (
        f"/ags/ms/cm-u-bk-ccpr/CcprHisNew?startDate={start}&endDate={end}"
        "&currency=USD/CNY"
    )
    response = _get(
        path,
        referer=f"{BASE}/english/bmkcpr/",
    )
    out: dict[str, dict[str, float]] = {}
    for rec in _records(response):
        record_date = _record_date(rec.get("date"), first, last)
        vals = rec.get("values") or []
        v = (
            _num(vals[0], ceiling=1000.0)
            if isinstance(vals, list) and 1 <= len(vals) <= 64
            else None
        )
        if record_date is not None and v is not None:
            out[record_date] = {"usdcny_parity": v}
    return FamilyCollection(
        values=out,
        raw_sha256=response.raw_sha256 if response else None,
        evidence_url=response.evidence_url if response else f"{BASE}{path}",
    )


def collect(start: str, end: str) -> ChinaEconCollection:
    """All three benchmark families merged per date. Slices that failed are
    simply absent — the caller can see which families reported."""
    merged: dict[str, dict[str, float]] = {}
    provenance: dict[str, FamilyCollection] = {}
    families = (
        ("shibor", fetch_shibor),
        ("repo_fixing", fetch_repo_fixings),
        ("central_parity", fetch_parity),
    )
    for i, (family, fetch) in enumerate(families):
        if i:
            time.sleep(SPACING_S)
        result = fetch(start, end)
        if result.values:
            if not result.raw_sha256:
                raise RuntimeError(f"{family} returned values without a raw response hash")
            provenance[family] = result
        for date, row in result.values.items():
            merged.setdefault(date, {}).update(row)
    return ChinaEconCollection(values=merged, provenance=provenance)
