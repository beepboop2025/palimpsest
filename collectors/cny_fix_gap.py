"""CNY fix gap — the market's dollar-yuan price against the state's daily fix.

Two prices exist for one currency. Every morning at 09:15 Beijing the PBOC
publishes the USD/CNY central parity — the price the state says is right,
and the centre of the ±2% band spot is allowed to trade in. The market then
spends the day agreeing or pulling against it. The gap between an
independent same-day market read and the fix is a daily, quantitative read
of that tension: persistent one-sided gaps are the market disputing the
state's price. This is the ECONOMIC BACKDROP family (like china-econ and
stock-connect): a policy read, not a censorship signal, and it deliberately
stays off the alarm board.

Legs, both keyless (probed live 2026-08-01):

  parity   this repo's own committed china-econ history — the fix as CFETS
           published it, already collected daily. No new egress.
  spot     ECB euro foreign exchange reference rates (eurofxref-daily.xml,
           ~16:00 CET each TARGET business day, concertation snapshot 14:15
           CET = 20:15 Beijing, same trading day as that morning's fix).
           USD/CNY is derived as EURCNY / EURUSD. Attribution: source ECB.
  check    Bank of Canada Valet series FXUSDCAD / FXCNYCAD (16:30 ET),
           second independent derivation — measured 1 pip from the ECB
           value on the probe date. Attribution: source Bank of Canada.

The offshore CNH leg is deliberately ABSENT: the only official daily CNH
print (TMA's USD/CNY(HK) fixing) is licence-restricted, and no clean keyless
source exists — stated rather than worked around.

Parse notes that will save the next reader an hour: the ECB daily file uses
SINGLE-quoted attributes and the -hist variants use double quotes, so the
regexes accept both; ECB still prints CNY on Chinese holidays, so a day with
no parity is a day with NO gap (the driver skips it rather than inventing a
fix). Standard-library only (shared safe transport + re + json), no dependencies in CI.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable
from datetime import date as calendar_date

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

USER_AGENT = "palimpsest.info observatory (fix-gap read; contact desk@palimpsest.info)"
MAX_BYTES = 2 * 1024 * 1024
MAX_ECB_RATES = 256
MAX_BOC_ROWS = 32
MAX_HISTORY_BYTES = 32 * 1024 * 1024
MAX_HISTORY_ROWS = 100_000

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
BOC_URL = "https://www.bankofcanada.ca/valet/observations/{series}/csv?recent=4"

_ECB_TIME = re.compile(r"""<Cube\s+time=['"](\d{4}-\d{2}-\d{2})['"]""")
_ECB_RATE = re.compile(r"""<Cube\s+currency=['"]([A-Z]{3})['"]\s+rate=['"]([\d.]+)['"]""")
_BOC_ROW = re.compile(r'"(\d{4}-\d{2}-\d{2})","([\d.]+)"')


def _get(
    url: str,
    timeout: float = 30.0,
    retries: int = 1,
    *,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> str | None:
    """Fail-soft page fetch: None on any error, empty body is an error."""
    reviewed = {
        ECB_URL,
        BOC_URL.format(series="FXCNYCAD"),
        BOC_URL.format(series="FXUSDCAD"),
    }
    if url not in reviewed:
        log.warning("cny_fix_gap refused an unreviewed endpoint")
        return None

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("central-bank request URL changed")

    for attempt in range(retries + 1):
        try:
            raw = fetcher(
                url,
                timeout=timeout,
                max_bytes=MAX_BYTES,
                max_redirects=0,
                headers={"Accept-Encoding": "identity", "User-Agent": USER_AGENT},
                url_policy=exact_url,
            )
            if len(raw) > MAX_BYTES:
                raise FetchError("central-bank response exceeded its byte budget")
            body = raw.decode("utf-8").strip()
            if not body:
                raise ValueError("empty body")
            return body
        except Exception as exc:  # noqa: BLE001 — abstain, never fake
            log.warning(
                "cny_fix_gap request attempt %d failed (%s)",
                attempt,
                type(exc).__name__,
            )
    return None


def parse_ecb(xml: str) -> dict | None:
    """{'date', 'eur_usd', 'eur_cny', 'usdcny'} from the daily file, or None.
    Both quote styles accepted (daily uses single, -hist uses double)."""
    if type(xml) is not str or not xml or len(xml.encode("utf-8")) > MAX_BYTES:
        return None
    t = _ECB_TIME.search(xml)
    if not t:
        return None
    matches = list(_ECB_RATE.finditer(xml))
    if not 1 <= len(matches) <= MAX_ECB_RATES:
        return None
    rates = {}
    for match in matches:
        try:
            value = float(match.group(2))
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(value) or not 0 < value <= 1_000_000:
            return None
        if match.group(1) in rates:
            return None
        rates[match.group(1)] = value
    usd, cny = rates.get("USD"), rates.get("CNY")
    if not usd or not cny:
        return None
    return {"date": t.group(1), "eur_usd": usd, "eur_cny": cny,
            "usdcny": round(cny / usd, 6)}


def parse_boc_csv(csv: str) -> dict[str, float]:
    """date -> value for one Valet CSV (quoted rows after the header block)."""
    if type(csv) is not str or not csv or len(csv.encode("utf-8")) > MAX_BYTES:
        return {}
    matches = list(_BOC_ROW.finditer(csv))
    if len(matches) > MAX_BOC_ROWS:
        return {}
    out = {}
    for match in matches:
        try:
            parsed_date = calendar_date.fromisoformat(match.group(1))
            value = float(match.group(2))
        except (ValueError, OverflowError):
            return {}
        if (
            parsed_date.isoformat() != match.group(1)
            or not math.isfinite(value)
            or not 0 < value <= 1_000_000
            or match.group(1) in out
        ):
            return {}
        out[match.group(1)] = value
    return out


def fetch_spot() -> dict | None:
    """ECB-derived USD/CNY plus the Bank of Canada cross-derivation when the
    dates line up. The ECB value is the published spot; the BoC value exists
    to catch a silent break in either source, not to average with."""
    xml = _get(ECB_URL)
    got = parse_ecb(xml) if xml else None
    if not got:
        return None
    cad_cny = _get(BOC_URL.format(series="FXCNYCAD"))
    cad_usd = _get(BOC_URL.format(series="FXUSDCAD"))
    if cad_cny and cad_usd:
        cny = parse_boc_csv(cad_cny)
        usd = parse_boc_csv(cad_usd)
        d = got["date"]
        if d in cny and d in usd and cny[d]:
            got["usdcny_boc"] = round(usd[d] / cny[d], 6)
            got["cross_check_diff"] = round(abs(got["usdcny"] - got["usdcny_boc"]), 6)
    return got


def read_parity(history_path: str) -> dict[str, float]:
    """date -> USD/CNY central parity from the committed china-econ history.
    Empty dict when the history is absent — the driver abstains."""
    out: dict[str, float] = {}
    try:
        if os.path.getsize(history_path) > MAX_HISTORY_BYTES:
            return {}
        with open(history_path, encoding="utf-8") as f:
            for row_number, line in enumerate(f, 1):
                if row_number > MAX_HISTORY_ROWS:
                    return {}
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                period, v = rec.get("date"), rec.get("usdcny_parity")
                if (
                    type(period) is str
                    and type(v) in (int, float)
                    and math.isfinite(float(v))
                    and 0 < float(v) <= 1_000
                ):
                    try:
                        parsed = calendar_date.fromisoformat(period)
                    except ValueError:
                        continue
                    if parsed.isoformat() == period:
                        out[period] = float(v)
    except OSError:
        return {}
    return out
