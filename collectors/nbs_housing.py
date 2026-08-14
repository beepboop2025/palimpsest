"""Pure parser for NBS 70-city new- and resale-home index tables."""
from __future__ import annotations

import calendar
import math
import re
from datetime import date

from bs4 import BeautifulSoup


PARSER_VERSION = "nbs-70-city-housing.html.v1"
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_TABLE_ROWS = 1_024
MAX_CITIES = 70


class NBSHousingParseError(ValueError):
    """The NBS document did not match the reviewed 70-city index shape."""


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("\u3000", "").replace("\xa0", ""))


def _soup(raw: bytes) -> BeautifulSoup:
    if type(raw) is not bytes or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        raise NBSHousingParseError("NBS housing HTML is empty or exceeds the byte bound")
    try:
        text = raw.decode("utf-8-sig", "strict")
    except UnicodeDecodeError as exc:
        raise NBSHousingParseError("NBS housing HTML is not strict UTF-8") from exc
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
        raise NBSHousingParseError("NBS response is an access-control interstitial")
    if "<html" not in folded and "<!doctype html" not in folded:
        raise NBSHousingParseError("NBS response is not HTML")
    return BeautifulSoup(text, "html.parser")


def _index(value: str, *, field: str) -> float:
    token = _clean(value)
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", token):
        raise NBSHousingParseError(f"NBS {field} is not a plain positive decimal")
    parsed = float(token)
    if not math.isfinite(parsed) or not 50.0 <= parsed <= 150.0:
        raise NBSHousingParseError(f"NBS {field} is outside the reviewed 50..150 index range")
    return parsed


def _preceding_table_title(table) -> str:
    """Return the nearest non-empty paragraph before a source table.

    NBS inserts an otherwise empty paragraph between every caption and table,
    so ``find_previous('p')`` alone points at spacing rather than semantics.
    """

    node = table
    for _ in range(8):
        node = node.find_previous("p")
        if node is None:
            break
        value = _clean(node.get_text(" ", strip=True))
        if value:
            return value
    return ""


def _city_groups(
    table,
    *,
    table_name: str,
    group_width: int,
) -> dict[str, tuple[float, float]]:
    rows = table.find_all("tr")
    if len(rows) > MAX_TABLE_ROWS:
        raise NBSHousingParseError(f"NBS {table_name} table exceeds the row bound")
    found: dict[str, tuple[float, float]] = {}
    for row in rows:
        cells = [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
        if not cells or len(cells) % group_width:
            continue
        for offset in range(0, len(cells), group_width):
            group = cells[offset : offset + group_width]
            city, mom_raw, yoy_raw = group[:3]
            if not city or not re.fullmatch(r"[\u3400-\u9fff]{2,12}", city):
                continue
            numeric = all(
                re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", token)
                for token in group[1:]
            )
            if not numeric:
                continue
            if city in found:
                raise NBSHousingParseError(f"NBS {table_name} repeats city {city}")
            # Validate an optional fixed-base/YTD-average column too.  It is not
            # emitted in this tranche, but a shifted column must not be mistaken
            # for a valid month-on-month or year-on-year index.
            mom = _index(mom_raw, field=f"{table_name}/{city} month index")
            yoy = _index(yoy_raw, field=f"{table_name}/{city} year index")
            if group_width == 4:
                _index(group[3], field=f"{table_name}/{city} auxiliary index")
            found[city] = (mom, yoy)
    if not 1 <= len(found) <= MAX_CITIES:
        raise NBSHousingParseError(
            f"NBS {table_name} must contain 1..{MAX_CITIES} complete city rows"
        )
    return found


def parse(raw: bytes) -> tuple[dict[str, object], ...]:
    """Parse city slices while retaining distinct new/resale and index bases."""

    soup = _soup(raw)
    page_text = _clean(soup.get_text(" ", strip=True))
    title = re.search(r"(20\d{2})年(\d{1,2})月份70个大中城市", page_text)
    if title is None:
        raise NBSHousingParseError("NBS housing release period/title shape changed")
    year, month = map(int, title.groups())
    if not 1 <= month <= 12:
        raise NBSHousingParseError("NBS housing release month is outside 1..12")

    candidates: dict[str, list[tuple[object, int]]] = {
        "new_home": [],
        "resale_home": [],
    }
    for table in soup.find_all("table"):
        text = _clean(table.get_text(" ", strip=True))
        context = _preceding_table_title(table) + text
        if "分类指数" in context:
            continue
        kind = None
        if "新建商品住宅销售价格指数" in context:
            kind = "new_home"
        elif "二手住宅销售价格指数" in context:
            kind = "resale_home"
        if kind is None:
            continue
        if "上月=100" not in text or "上年同月=100" not in text:
            raise NBSHousingParseError(f"NBS {kind} index headers changed")
        has_auxiliary = "上年同期=100" in text or "定基" in text
        candidates[kind].append((table, 4 if has_auxiliary else 3))
    if any(not 1 <= len(tables) <= 2 for tables in candidates.values()):
        raise NBSHousingParseError(
            "NBS housing document must contain one reviewed new-home and one "
            "reviewed resale-home table, with at most one identical responsive copy"
        )
    by_kind: dict[str, dict[str, tuple[float, float]]] = {}
    for kind, tables in candidates.items():
        parsed_tables = [
            _city_groups(table, table_name=kind, group_width=group_width)
            for table, group_width in tables
        ]
        if any(value != parsed_tables[0] for value in parsed_tables[1:]):
            raise NBSHousingParseError(
                f"NBS {kind} responsive table copies disagree"
            )
        by_kind[kind] = parsed_tables[0]
    if set(by_kind["new_home"]) != set(by_kind["resale_home"]):
        raise NBSHousingParseError("NBS new-home and resale-home city panels differ")

    end = date(year, month, calendar.monthrange(year, month)[1])
    start = date(year, month, 1)
    out: list[dict[str, object]] = []
    table_ids = {
        "new_home": "nbs_70_city_new_home_index_table",
        "resale_home": "nbs_70_city_resale_home_index_table",
    }
    for kind in ("new_home", "resale_home"):
        for city, (mom, yoy) in sorted(by_kind[kind].items()):
            common = {
                "frequency": "M",
                "period_start": start,
                "period_end": end,
                "aggregation_window": "month",
                "geography_key": city,
                "sector_key": kind,
                "source_table_id": table_ids[kind],
            }
            out.extend(
                (
                    {
                        **common,
                        "series_key": f"{kind}_mom_index",
                        "value": mom,
                        "source_unit": "上月=100",
                    },
                    {
                        **common,
                        "series_key": f"{kind}_yoy_index",
                        "value": yoy,
                        "source_unit": "上年同月=100",
                    },
                )
            )
    return tuple(out)


__all__ = ["NBSHousingParseError", "PARSER_VERSION", "parse"]
