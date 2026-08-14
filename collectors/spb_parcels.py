"""Pure parser for the SPB national postal-development HTML table."""
from __future__ import annotations

import calendar
import math
import re
from datetime import date

from bs4 import BeautifulSoup


PARSER_VERSION = "spb-parcels.html.v1"
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_TABLE_ROWS = 1_024


class SPBParcelsParseError(ValueError):
    """The SPB release did not match the reviewed national aggregate shape."""


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("\u3000", "").replace("\xa0", ""))


def _soup(raw: bytes) -> BeautifulSoup:
    if type(raw) is not bytes or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise SPBParcelsParseError("SPB HTML is empty or exceeds the byte bound")
    try:
        text = raw.decode("utf-8-sig", "strict")
    except UnicodeDecodeError as exc:
        raise SPBParcelsParseError("SPB HTML is not strict UTF-8") from exc
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
        raise SPBParcelsParseError("SPB response is an access-control interstitial")
    if "<html" not in folded and "<!doctype html" not in folded:
        raise SPBParcelsParseError("SPB response is not HTML")
    return BeautifulSoup(text, "html.parser")


def _number(value: str, *, field: str, low: float, high: float) -> float:
    token = _clean(value).replace("−", "-").replace("—", "-")
    if not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", token):
        raise SPBParcelsParseError(f"SPB {field} is not a plain decimal")
    parsed = float(token)
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise SPBParcelsParseError(f"SPB {field} is outside the reviewed range")
    return parsed


def _metric_rows(
    cells: list[str],
    *,
    year: int,
    month: int,
) -> list[dict[str, object]]:
    if len(cells) != 6:
        raise SPBParcelsParseError("SPB express-business row changed column count")
    label, unit, ytd_value, month_value, ytd_yoy, month_yoy = cells
    if not re.fullmatch(r"(?:\d+[、.]?)?快递业务", _clean(label)):
        raise SPBParcelsParseError("SPB express-business label changed")
    normalized_unit = _clean(unit)
    metric = {"亿元": "revenue", "亿件": "volume"}.get(normalized_unit)
    if metric is None:
        raise SPBParcelsParseError(f"SPB express-business unit changed: {unit!r}")
    last_day = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    value_ceiling = 10_000_000 if metric == "revenue" else 100_000_000
    common = {
        "frequency": "M",
        "geography_key": "national",
        "sector_key": "express_delivery",
        "source_table_id": "spb_national_postal_development_table",
    }
    return [
        {
            **common,
            "series_key": f"express_{metric}_month",
            "value": _number(
                month_value,
                field=f"monthly {metric}",
                low=0,
                high=value_ceiling,
            ),
            "source_unit": normalized_unit,
            "period_start": month_start,
            "period_end": month_end,
            "aggregation_window": "month",
        },
        {
            **common,
            "series_key": f"express_{metric}_ytd",
            "value": _number(
                ytd_value,
                field=f"YTD {metric}",
                low=0,
                high=value_ceiling,
            ),
            "source_unit": normalized_unit,
            "period_start": date(year, 1, 1),
            "period_end": month_end,
            "aggregation_window": "year_to_date",
        },
        {
            **common,
            "series_key": f"express_{metric}_yoy_month",
            "value": _number(
                month_yoy,
                field=f"monthly {metric} YoY",
                low=-100,
                high=1_000,
            ),
            "source_unit": "%",
            "period_start": month_start,
            "period_end": month_end,
            "aggregation_window": "month",
        },
        {
            **common,
            "series_key": f"express_{metric}_yoy_ytd",
            "value": _number(
                ytd_yoy,
                field=f"YTD {metric} YoY",
                low=-100,
                high=1_000,
            ),
            "source_unit": "%",
            "period_start": date(year, 1, 1),
            "period_end": month_end,
            "aggregation_window": "year_to_date",
        },
    ]


def parse(raw: bytes) -> tuple[dict[str, object], ...]:
    """Parse volume/revenue level and YoY rows, keeping month and YTD apart."""

    soup = _soup(raw)
    page_text = _clean(soup.get_text(" ", strip=True))
    year_match = re.search(r"(20\d{2})年邮政行业运行情况", page_text)
    if year_match is None:
        raise SPBParcelsParseError("SPB release year/title shape changed")
    year = int(year_match.group(1))

    target_rows: list[list[str]] = []
    months: set[int] = set()
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) > MAX_TABLE_ROWS:
            raise SPBParcelsParseError("SPB table exceeds the row bound")
        table_text = _clean(table.get_text(" ", strip=True))
        required = ("指标名称", "单位", "累计", "当月", "比去年同期增长")
        if not all(token in table_text for token in required):
            continue
        month_match = re.search(r"(\d{1,2})月份", table_text)
        if month_match:
            months.add(int(month_match.group(1)))
        for row in rows:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if cells and re.fullmatch(r"(?:\d+[、.]?)?快递业务", _clean(cells[0])):
                target_rows.append(cells)
    if len(months) != 1:
        raise SPBParcelsParseError("SPB table must declare exactly one current month")
    month = next(iter(months))
    if not 1 <= month <= 12:
        raise SPBParcelsParseError("SPB current month is outside 1..12")
    if len(target_rows) != 2:
        raise SPBParcelsParseError(
            "SPB national table must contain exactly one express revenue row and one "
            "express volume row"
        )
    units = {_clean(row[1]) for row in target_rows if len(row) >= 2}
    if units != {"亿元", "亿件"}:
        raise SPBParcelsParseError(f"SPB national express units changed: {sorted(units)}")
    out: list[dict[str, object]] = []
    for row in target_rows:
        out.extend(_metric_rows(row, year=year, month=month))
    return tuple(out)


__all__ = ["PARSER_VERSION", "SPBParcelsParseError", "parse"]
