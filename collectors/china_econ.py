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
well apart, and the git-committed history file is the long record: each run
appends only data-dates not already recorded, so the archive outgrows the
API's window the same way the DDTI history does.

Standard-library only (urllib + json), no dependencies in CI.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

BASE = "https://www.chinamoney.com.cn"
USER_AGENT = "palimpsest.info observatory (official-benchmark ingest; contact desk@palimpsest.info)"
SPACING_S = 20.0   # between the three calls — the portal punishes bursts

SHIBOR_TENORS = ["ON", "1W", "2W", "1M", "3M", "6M", "9M", "1Y"]
FRR_KEYS = ["FR001", "FR007", "FR014", "FDR001", "FDR007", "FDR014"]


def _get(path: str, referer: str, timeout: float = 30.0, retries: int = 2) -> dict | None:
    """One portal call. Fail-soft: None on any error, and an EMPTY body counts
    as an error (that is the throttle speaking, not a data statement)."""
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": referer,
    })
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace").strip()
            if not body:
                raise ValueError("empty body (throttled)")
            return json.loads(body)
        except Exception as exc:  # noqa: BLE001 — abstain, never fake
            log.warning("chinamoney %s attempt %d failed: %s", path, attempt, exc)
            if attempt < retries:
                time.sleep(30.0 * (attempt + 1))
    return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_shibor(start: str, end: str) -> dict[str, dict[str, float]]:
    """date -> {shibor_on: x, shibor_1w: x, ...}. Empty dict on failure."""
    d = _get(
        f"/ags/ms/cm-u-bk-shibor/ShiborHis?lang=en&startDate={start}&endDate={end}",
        referer=f"{BASE}/english/bmkshibor/",
    )
    out: dict[str, dict[str, float]] = {}
    for rec in (d or {}).get("records", []):
        date = rec.get("showDateCN")
        if not date:
            continue
        row = {}
        for tenor in SHIBOR_TENORS:
            v = _num(rec.get(tenor))
            if v is not None:
                row[f"shibor_{tenor.lower()}"] = v
        if row:
            out[str(date)[:10]] = row
    return out


def fetch_repo_fixings(start: str, end: str) -> dict[str, dict[str, float]]:
    """date -> {fr001: x, fdr007: x, ...}. FDR = depository-institutions repo
    fixing, the closest public daily proxy to the DR007 policy anchor."""
    d = _get(
        f"/ags/ms/cm-u-bk-currency/FrrHis?lang=EN&startDate={start}&endDate={end}",
        referer=f"{BASE}/english/bmkfrr/",
    )
    out: dict[str, dict[str, float]] = {}
    for rec in (d or {}).get("records", []):
        date = rec.get("lfiProducDate")
        vals = rec.get("frValueMap") or {}
        if not date:
            continue
        row = {}
        for key in FRR_KEYS:
            v = _num(vals.get(key))
            if v is not None:
                row[key.lower()] = v
        if row:
            out[str(date)[:10]] = row
    return out


def fetch_parity(start: str, end: str) -> dict[str, dict[str, float]]:
    """date -> {usdcny_parity: x} — the daily central parity fix."""
    d = _get(
        f"/ags/ms/cm-u-bk-ccpr/CcprHisNew?startDate={start}&endDate={end}&currency=USD/CNY",
        referer=f"{BASE}/english/bmkcpr/",
    )
    out: dict[str, dict[str, float]] = {}
    for rec in (d or {}).get("records", []):
        date = rec.get("date")
        vals = rec.get("values") or []
        v = _num(vals[0]) if vals else None
        if date and v is not None:
            out[str(date)[:10]] = {"usdcny_parity": v}
    return out


def collect(start: str, end: str) -> dict[str, dict[str, float]]:
    """All three benchmark families merged per date. Slices that failed are
    simply absent — the caller can see which families reported."""
    merged: dict[str, dict[str, float]] = {}
    for i, fetch in enumerate((fetch_shibor, fetch_repo_fixings, fetch_parity)):
        if i:
            time.sleep(SPACING_S)
        for date, row in fetch(start, end).items():
            merged.setdefault(date, {}).update(row)
    return merged
