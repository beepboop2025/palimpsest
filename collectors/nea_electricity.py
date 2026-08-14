"""Pure parser for NEA monthly sector electricity-consumption prose."""
from __future__ import annotations

import calendar
import math
import re
from datetime import date

from bs4 import BeautifulSoup


PARSER_VERSION = "nea-electricity.html.v1"
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_PARAGRAPHS = 2_000

_SECTORS = (
    ("全社会", "all_electricity"),
    ("第一产业", "primary_industry"),
    ("第二产业", "secondary_industry"),
    ("第三产业", "tertiary_industry"),
    ("城乡居民生活", "households"),
)


class NEAElectricityParseError(ValueError):
    """The NEA release did not match the reviewed sector prose grammar."""


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("\u3000", "").replace("\xa0", ""))


def _soup(raw: bytes) -> BeautifulSoup:
    if type(raw) is not bytes or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise NEAElectricityParseError("NEA HTML is empty or exceeds the byte bound")
    try:
        text = raw.decode("utf-8-sig", "strict")
    except UnicodeDecodeError as exc:
        raise NEAElectricityParseError("NEA HTML is not strict UTF-8") from exc
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
        raise NEAElectricityParseError("NEA response is an access-control interstitial")
    if "<html" not in folded and "<!doctype html" not in folded:
        raise NEAElectricityParseError("NEA response is not HTML")
    return BeautifulSoup(text, "html.parser")


def _decimal(value: str, *, field: str, low: float, high: float) -> float:
    token = value.replace("−", "-").replace("—", "-")
    if not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", token):
        raise NEAElectricityParseError(f"NEA {field} is not a plain decimal")
    parsed = float(token)
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise NEAElectricityParseError(f"NEA {field} is outside the reviewed range")
    return parsed


def _sector_values(paragraph: str, label: str) -> tuple[float, float]:
    pattern = re.compile(
        re.escape(label)
        + r"用电量(?:累计)?(?P<level>(?:0|[1-9]\d*)(?:\.\d+)?)亿千瓦时[，,]"
        + r"同比(?P<direction>增长|下降)(?P<yoy>(?:0|[1-9]\d*)(?:\.\d+)?)[%％]"
    )
    matches = list(pattern.finditer(paragraph))
    if len(matches) != 1:
        raise NEAElectricityParseError(
            f"NEA paragraph must contain exactly one complete {label} observation"
        )
    match = matches[0]
    level = _decimal(match.group("level"), field=f"{label} consumption", low=0, high=10_000_000)
    yoy = _decimal(match.group("yoy"), field=f"{label} YoY", low=0, high=1_000)
    if match.group("direction") == "下降":
        yoy = -yoy
    return level, yoy


def _rows_for_window(
    paragraph: str,
    *,
    year: int,
    month: int,
    aggregation_window: str,
) -> list[dict[str, object]]:
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    start = date(year, month, 1) if aggregation_window == "month" else date(year, 1, 1)
    suffix = "month" if aggregation_window == "month" else "ytd"
    out: list[dict[str, object]] = []
    for label, sector_key in _SECTORS:
        level, yoy = _sector_values(paragraph, label)
        common = {
            "frequency": "M",
            "period_start": start,
            "period_end": end,
            "aggregation_window": aggregation_window,
            "geography_key": "national",
            "sector_key": sector_key,
            "source_table_id": "nea_electricity_release_prose",
        }
        out.extend(
            (
                {
                    **common,
                    "series_key": f"electricity_consumption_{suffix}",
                    "value": level,
                    "source_unit": "亿千瓦时",
                },
                {
                    **common,
                    "series_key": f"electricity_consumption_yoy_{suffix}",
                    "value": yoy,
                    "source_unit": "%",
                },
            )
        )
    return out


def parse(raw: bytes) -> tuple[dict[str, object], ...]:
    """Parse the five reviewed national sectors for month and year-to-date."""

    soup = _soup(raw)
    page_text = _clean(soup.get_text(" ", strip=True))
    title = re.search(r"(20\d{2})年(\d{1,2})月份全社会用电量", page_text)
    if title is None:
        raise NEAElectricityParseError("NEA release period/title shape changed")
    year, month = map(int, title.groups())
    if not 1 <= month <= 12:
        raise NEAElectricityParseError("NEA release month is outside 1..12")
    paragraphs = [_clean(node.get_text(" ", strip=True)) for node in soup.find_all("p")]
    if len(paragraphs) > MAX_PARAGRAPHS:
        raise NEAElectricityParseError("NEA document exceeds the paragraph bound")
    monthly = [
        value
        for value in paragraphs
        if re.match(rf"^{month}月份[，,]全社会用电量", value)
    ]
    ytd = [
        value
        for value in paragraphs
        if re.match(rf"^1[～~—–\-至]{month}月[，,]全社会用电量", value)
    ]
    if len(monthly) != 1 or len(ytd) != 1:
        raise NEAElectricityParseError(
            "NEA document must contain exactly one monthly and one YTD aggregate paragraph"
        )
    return tuple(
        _rows_for_window(
            monthly[0], year=year, month=month, aggregation_window="month"
        )
        + _rows_for_window(
            ytd[0], year=year, month=month, aggregation_window="year_to_date"
        )
    )


__all__ = ["NEAElectricityParseError", "PARSER_VERSION", "parse"]
