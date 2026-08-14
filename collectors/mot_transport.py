"""Pure parser for reviewed MOT inline urban-passenger release tables.

The current MOT release family sometimes publishes only an image and XLSX
link.  This parser intentionally refuses those landing pages: exact HTML bytes
must contain the reviewed numeric table shape before an observation can exist.
"""
from __future__ import annotations

import calendar
import math
import re
from datetime import date

from bs4 import BeautifulSoup


PARSER_VERSION = "mot-transport.html.v1"
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_TABLE_ROWS = 512


class MOTTransportParseError(ValueError):
    """The MOT document did not match the reviewed aggregate table shape."""


def _soup(raw: bytes) -> BeautifulSoup:
    if type(raw) is not bytes or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise MOTTransportParseError("MOT HTML is empty or exceeds the byte bound")
    try:
        text = raw.decode("utf-8-sig", "strict")
    except UnicodeDecodeError as exc:
        raise MOTTransportParseError("MOT HTML is not strict UTF-8") from exc
    folded = text.casefold()
    if any(
        marker in folded
        for marker in (
            "captcha",
            "access denied",
            "verify you are human",
            "安全验证",
            "访问验证",
        )
    ):
        raise MOTTransportParseError("MOT response is an access-control interstitial")
    if "<html" not in folded and "<!doctype html" not in folded:
        raise MOTTransportParseError("MOT response is not HTML")
    return BeautifulSoup(text, "html.parser")


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("\u3000", "").replace("\xa0", ""))


def _number(value: str, *, field: str, low: float, high: float) -> float:
    token = _clean(value).replace("−", "-").replace("—", "-")
    if not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", token):
        raise MOTTransportParseError(f"MOT {field} is not a plain decimal")
    parsed = float(token)
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise MOTTransportParseError(f"MOT {field} is outside the reviewed range")
    return parsed


def _row(cell_values: list[str], *, year: int, month: int) -> list[dict[str, object]]:
    if len(cell_values) != 6:
        raise MOTTransportParseError("MOT passenger row changed column count")
    label, unit, month_value, ytd_value, month_yoy, ytd_yoy = cell_values
    if _clean(label) != "城市客运量":
        raise MOTTransportParseError("MOT passenger row label changed")
    if _clean(unit) != "亿人次":
        raise MOTTransportParseError(f"MOT passenger unit changed: {unit!r}")
    last_day = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    common = {
        "source_unit": "亿人次",
        "frequency": "M",
        "geography_key": "national",
        "sector_key": "urban_passenger",
        "source_table_id": "mot_urban_passenger_release_table",
    }
    return [
        {
            **common,
            "series_key": "urban_passenger_volume_month",
            "value": _number(month_value, field="monthly volume", low=0, high=100_000),
            "period_start": month_start,
            "period_end": month_end,
            "aggregation_window": "month",
        },
        {
            **common,
            "series_key": "urban_passenger_volume_ytd",
            "value": _number(ytd_value, field="YTD volume", low=0, high=1_000_000),
            "period_start": date(year, 1, 1),
            "period_end": month_end,
            "aggregation_window": "year_to_date",
        },
        {
            **common,
            "source_unit": "%",
            "series_key": "urban_passenger_yoy_month",
            "value": _number(month_yoy, field="monthly YoY", low=-100, high=1_000),
            "period_start": month_start,
            "period_end": month_end,
            "aggregation_window": "month",
        },
        {
            **common,
            "source_unit": "%",
            "series_key": "urban_passenger_yoy_ytd",
            "value": _number(ytd_yoy, field="YTD YoY", low=-100, high=1_000),
            "period_start": date(year, 1, 1),
            "period_end": month_end,
            "aggregation_window": "year_to_date",
        },
    ]


def parse(raw: bytes) -> tuple[dict[str, object], ...]:
    """Return reviewed aggregate rows without performing I/O or inference."""

    soup = _soup(raw)
    text = _clean(soup.get_text(" ", strip=True))
    match = re.search(r"(20\d{2})年1[-—–~～至](\d{1,2})月全国城市客运量", text)
    if match is None:
        raise MOTTransportParseError("MOT release period/title shape changed")
    year, month = map(int, match.groups())
    if not 1 <= month <= 12:
        raise MOTTransportParseError("MOT release month is outside 1..12")

    matches: list[list[str]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) > MAX_TABLE_ROWS:
            raise MOTTransportParseError("MOT table exceeds the row bound")
        table_text = _clean(table.get_text(" ", strip=True))
        required = ("指标", "单位", "当月", "累计", "当月同比", "累计同比")
        if not all(token in table_text for token in required):
            continue
        for row in rows:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if cells and _clean(cells[0]) == "城市客运量":
                matches.append(cells)
    if len(matches) != 1:
        raise MOTTransportParseError(
            "MOT HTML must contain exactly one reviewed inline passenger row; "
            "image/XLSX-only releases abstain"
        )
    return tuple(_row(matches[0], year=year, month=month))


__all__ = ["MOTTransportParseError", "PARSER_VERSION", "parse"]
