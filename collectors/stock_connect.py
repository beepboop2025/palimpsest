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

Standard-library only (urllib + json), no dependencies in CI.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

URL = "https://www.hkex.com.hk/eng/csm/DailyStat/data_tab_daily_{yyyymmdd}e.js"
USER_AGENT = "palimpsest.info observatory (official-statistics ingest; contact desk@palimpsest.info)"
SPACING_S = 1.2   # polite gap between per-day fetches on backfill walks


def _get_raw(yyyymmdd: str, timeout: float = 30.0) -> str | None:
    """Fetch one day's file. Fail-soft: None on any transport error."""
    req = urllib.request.Request(
        URL.format(yyyymmdd=yyyymmdd), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — abstain, never fake
        log.warning("hkex %s fetch failed: %s", yyyymmdd, exc)
        return None


def _num(cell: str) -> float | None:
    cell = cell.strip().replace(",", "")
    if not cell or cell in {"-", "N/A"}:
        return None
    try:
        return float(cell)
    except ValueError:
        return None


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
    except (json.JSONDecodeError, IndexError):
        return None

    date: str | None = None
    per_market: dict[str, dict[str, float]] = {}
    for market in data:
        name = market.get("market", "")
        date = date or market.get("date")
        content = market.get("content") or []
        if not content:
            continue
        table = content[0].get("table", {})
        schema = (table.get("schema") or [[]])[0]
        rows = table.get("tr") or []
        vals: dict[str, float] = {}
        for i, col in enumerate(schema):
            if i >= len(rows):
                break
            v = _num(rows[i]["td"][0][0])
            if v is not None:
                vals[col] = v
        if vals:
            per_market[name] = vals

    if date is None or not per_market:
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
    out: dict[str, dict] = {}
    for i, d in enumerate(dates):
        if i:
            time.sleep(spacing_s)
        row = collect_day(d)
        if row:
            out[row["date"]] = row
    return out
