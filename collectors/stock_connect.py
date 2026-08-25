"""Stock Connect daily flows — the cross-border door, read from HKEX's
own daily-statistics prints.

Palimpsest reads what the Chinese state publishes, hides, and deletes.
This collector reads a PUBLISHED-then-NARROWED record: HKEX printed full
northbound buy/sell turnover (net foreign flow into A-shares) until
August 2024, when the northbound direction was discontinued — only total
turnover survives. Southbound (mainland money into HK) still carries the
full buy/sell split. So the honest daily read is: southbound NET flow,
plus turnover-only activity for northbound. The narrowing itself is part
of the record and is stated in every published reading; the missing
northbound net is never estimated or faked.

Vantage notes (probed live 2026-07-17): the per-day file
``data_tab_daily_YYYYMMDDe.js`` is keyless and serves international
traffic; non-trading days and dates outside retention return an HTML
error page, not JSON. Retention is shallow (roughly the current calendar
year — Jan-2026 files exist, all 2025 files are gone), so the
git-committed history file is the long record, same as china-econ.

Units: HKEX prints millions — CNY for northbound, HKD for southbound.
Published fields are billions (``*_b``), currency per leg as above.

Standard-library only (shared safe transport + json), no dependencies in CI.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable
from datetime import datetime

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

URL = "https://www.hkex.com.hk/eng/csm/DailyStat/data_tab_daily_{yyyymmdd}e.js"
USER_AGENT = "palimpsest.info observatory (official-statistics ingest; contact desk@palimpsest.info)"
SPACING_S = 1.2   # polite gap between per-day fetches on backfill walks
MAX_BYTES = 4 * 1024 * 1024
MAX_DATES_PER_RUN = 400
_DATE = re.compile(r"\d{8}\Z")


def _valid_day(yyyymmdd: object) -> bool:
    if type(yyyymmdd) is not str or _DATE.fullmatch(yyyymmdd) is None:
        return False
    try:
        datetime.strptime(yyyymmdd, "%Y%m%d")
    except ValueError:
        return False
    return True


def _get_raw(
    yyyymmdd: str,
    timeout: float = 30.0,
    *,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> str | None:
    """Fetch one exact bounded daily file, abstaining on any refusal."""
    if not _valid_day(yyyymmdd):
        log.warning("hkex refused an invalid daily-stat date")
        return None
    url = URL.format(yyyymmdd=yyyymmdd)

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("HKEX daily-stat URL changed")

    try:
        payload = fetcher(
            url,
            timeout=timeout,
            max_bytes=MAX_BYTES,
            max_redirects=0,
            headers={
                "Accept": "application/javascript,application/json,text/plain;q=0.5",
                "User-Agent": USER_AGENT,
            },
            url_policy=exact_url,
        )
        if len(payload) > MAX_BYTES:
            raise FetchError("HKEX response exceeded its byte budget")
        return payload.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — abstain, never fake
        log.warning("hkex daily-stat fetch failed (%s)", type(exc).__name__)
        return None


def _num(cell: str) -> float | None:
    cell = cell.strip().replace(",", "")
    if not cell or cell in {"-", "N/A"}:
        return None
    try:
        value = float(cell)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_daily(raw: str) -> dict | None:
    """One data_tab_daily payload -> {date, southbound_net_b, ...} or None.

    The payload is a JS assignment ``tabData = [...]``; each market block
    carries a TRANSPOSED summary table (schema[0][i] names row i). A
    non-trading day / out-of-retention date serves an HTML page instead —
    that returns None, a statement of absence, not zero.
    """
    raw = raw.strip()
    if not raw.startswith("tabData"):
        return None
    try:
        data = json.loads(raw.split("=", 1)[1].rstrip().rstrip(";"))
    except (json.JSONDecodeError, IndexError, RecursionError):
        return None
    if not isinstance(data, list) or len(data) > 16:
        return None

    date: str | None = None
    per_market: dict[str, dict[str, float]] = {}
    for market in data:
        if not isinstance(market, dict):
            continue
        name = market.get("market", "")
        date = date or market.get("date")
        content = market.get("content") or []
        if not isinstance(content, list) or not content or len(content) > 4:
            continue
        if not isinstance(content[0], dict):
            continue
        table = content[0].get("table", {})
        if not isinstance(table, dict):
            continue
        schema = (table.get("schema") or [[]])[0]
        rows = table.get("tr") or []
        if (
            not isinstance(schema, list)
            or not isinstance(rows, list)
            or len(schema) > 64
            or len(rows) > 64
        ):
            continue
        vals: dict[str, float] = {}
        for i, col in enumerate(schema):
            if i >= len(rows):
                break
            try:
                cell = rows[i]["td"][0][0]
            except (KeyError, IndexError, TypeError):
                continue
            if not isinstance(col, str) or not isinstance(cell, str):
                continue
            v = _num(cell)
            if v is not None:
                vals[col] = v
        if vals:
            per_market[name] = vals

    if (
        not isinstance(date, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", date[:10]) is None
        or not per_market
    ):
        return None

    def bn(market: str, col: str) -> float | None:
        v = per_market.get(market, {}).get(col)
        return round(v / 1000.0, 3) if v is not None else None

    out: dict[str, float | str] = {"date": str(date)[:10]}

    # Southbound: full buy/sell split still published (HKD).
    sb_buy = [bn(m, "Buy Turnover") for m in ("SSE Southbound", "SZSE Southbound")]
    sb_sell = [bn(m, "Sell Turnover") for m in ("SSE Southbound", "SZSE Southbound")]
    if all(v is not None for v in sb_buy + sb_sell):
        buy, sell = sum(sb_buy), sum(sb_sell)
        out["sb_buy_b"] = round(buy, 3)
        out["sb_sell_b"] = round(sell, 3)
        out["southbound_net_b"] = round(buy - sell, 3)

    # Northbound: turnover only — the direction print died Aug-2024.
    nb_sse = bn("SSE Northbound", "Total Turnover")
    nb_szse = bn("SZSE Northbound", "Total Turnover")
    if nb_sse is not None:
        out["nb_sse_turnover_b"] = nb_sse
    if nb_szse is not None:
        out["nb_szse_turnover_b"] = nb_szse
    if nb_sse is not None and nb_szse is not None:
        out["nb_turnover_b"] = round(nb_sse + nb_szse, 3)

    # A date with neither leg is not a reading.
    if len(out) == 1:
        return None
    return out


def collect_day(yyyymmdd: str) -> dict | None:
    raw = _get_raw(yyyymmdd)
    if raw is None:
        return None
    return parse_daily(raw)


def collect_range(dates: list[str], spacing_s: float = SPACING_S) -> dict[str, dict]:
    """Fetch a list of YYYYMMDD dates politely; skip absent days silently
    (weekends/holidays are absence, not error)."""
    if (
        not isinstance(dates, list)
        or not 1 <= len(dates) <= MAX_DATES_PER_RUN
        or any(not _valid_day(day) for day in dates)
        or not isinstance(spacing_s, (int, float))
        or isinstance(spacing_s, bool)
        or not 0 <= spacing_s <= 60
    ):
        log.warning("hkex refused an invalid or oversized collection window")
        return {}
    out: dict[str, dict] = {}
    for i, d in enumerate(dates):
        if i:
            time.sleep(spacing_s)
        row = collect_day(d)
        if row:
            out[row["date"]] = row
    return out
