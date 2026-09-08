"""Bounded NBS release discovery and lossless statistical-table extraction.

This is a source-table dataset, not a replacement for the reviewed economic
observation ledger. Original headers, missing tokens and row labels travel with
every value; annual, cumulative and monthly columns are never merged.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from core.safe_fetch import safe_fetch_bytes

PARSER_VERSION = "nbs-release-tables.v3"
INDEX_URL = "https://www.stats.gov.cn/english/PressRelease/"
TERMS_URL = "https://www.stats.gov.cn/english/nbs/200701/t20070104_59236.html"
MAX_BYTES = 4 * 1024 * 1024
FAMILIES = {
    "pmi": ("Business surveys", "purchasing managers"),
    "industrial_profits": ("Firm finances", "profits of industrial enterprises"),
    "industrial_production": ("Industrial activity", "industrial production operation"),
    "fixed_investment": ("Capital expenditure", "investment in fixed assets"),
    "property_investment": ("Property development", "investment in real estate"),
    "retail": ("Household demand", "total retail sales"),
    "energy": ("Energy production", "energy production"),
    "housing": ("70-city housing", "sales prices of commercial residential"),
    "consumer_prices": ("Consumer prices", "consumer price index"),
    "producer_prices": ("Producer prices", "industrial producer price"),
    "commodities": ("Commodity prices", "market prices of important means"),
    "national_accounts": ("National accounts", "gross domestic product"),
}
_NUMBER = re.compile(r"^[+−–-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$")
_MISSING = {"", "-", "—", "–", "…", "...", "n/a", "na", "null"}


class NBSReleaseError(ValueError):
    """Unexpected source shape; retain the last good capture and report failure."""


def clean(value: str) -> str:
    return " ".join(value.replace("\u200b", "").replace("\ufeff", "").split())


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def source_policy(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "www.stats.gov.cn"
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not parsed.path.startswith("/english/PressRelease/")
            or parsed.query or parsed.fragment):
        raise NBSReleaseError("URL is outside the reviewed NBS release shelf")


def fetch(url: str) -> bytes:
    return safe_fetch_bytes(url, max_bytes=MAX_BYTES, timeout=25,
                            url_policy=source_policy,
                            headers={"User-Agent": "PalimpsestResearch/1.0 (+https://www.palimpsest.info/)"})


def soup_from(raw: bytes) -> BeautifulSoup:
    if not raw or len(raw) > MAX_BYTES:
        raise NBSReleaseError("empty or oversized NBS document")
    text = raw.decode("utf-8-sig", errors="strict")
    if any(x in text.lower() for x in ("access denied", "verify you are human", "captcha", "安全验证")):
        raise NBSReleaseError("NBS returned an access-control page")
    return BeautifulSoup(text, "html.parser")


def classify(title: str) -> str | None:
    folded = title.casefold().replace("’", "'")
    return next((key for key, (_, term) in FAMILIES.items() if term in folded), None)


def discover(raw: bytes, url: str = INDEX_URL) -> list[dict]:
    source_policy(url)
    found = {}
    for anchor in soup_from(raw).select("ul.list a[href]"):
        target = urljoin(url, anchor["href"])
        if not re.fullmatch(r"https://www\.stats\.gov\.cn/english/PressRelease/\d{6}/t\d{8}_\d+\.html", target):
            continue
        title = clean(anchor.get("title") or anchor.get_text(" ", strip=True))
        family = classify(title)
        if family:
            found[target] = {"url": target, "title": re.sub(r"^\d+\.", "", title), "family": family}
    if not found:
        raise NBSReleaseError("NBS index has no recognized statistical releases")
    return list(found.values())


def number(token: str) -> float | None:
    token = clean(token)
    if not _NUMBER.fullmatch(token):
        return None
    result = float(token.rstrip("%").replace(",", "").replace("−", "-").replace("–", "-"))
    if not math.isfinite(result):
        raise NBSReleaseError("non-finite statistical value")
    return result


def expand_table(table) -> list[list[str]]:
    """Expand HTML spans so a value remains under its complete source heading."""
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    if not 1 <= len(rows) <= 1024:
        raise NBSReleaseError("table row bound exceeded")
    occupied: dict[tuple[int, int], str] = {}
    for r, row in enumerate(rows):
        c = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while (r, c) in occupied:
                c += 1
            try:
                height, width = int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))
            except ValueError as exc:
                raise NBSReleaseError("invalid table span") from exc
            if not 1 <= height <= 1024 or not 1 <= width <= 40 or c + width > 40 or r + height > len(rows):
                raise NBSReleaseError("table span exceeds bounds")
            value = clean(cell.get_text(" ", strip=True))
            for rr in range(r, r + height):
                for cc in range(c, c + width):
                    if (rr, cc) in occupied:
                        raise NBSReleaseError("overlapping table cells")
                    occupied[rr, cc] = value
            c += width
    width = max((c + 1 for _, c in occupied), default=0)
    return [[occupied.get((r, c), "") for c in range(width)] for r in range(len(rows))]


def table_context(table) -> str:
    values = []
    container = table
    if table.parent.get("class") == ["ue_table"]:
        container = table.parent
    for node in container.previous_siblings:
        if getattr(node, "name", None) == "table" or (hasattr(node, "find") and not isinstance(node, str) and node.find("table")):
            break
        if not hasattr(node, "get_text"):
            continue
        value = clean(node.get_text(" ", strip=True))
        if value:
            values.append(value)
        if len(values) == 3:
            break
    captions = [value for value in reversed(values) if re.match(r"^(?:Table\b|Sales Price|Unit:|Key Financial|Performance Indicators)", value, re.I)]
    return " | ".join(captions)[-1200:]


def extract_table(table, ordinal: int) -> dict | None:
    grid = expand_table(table)
    # A data row has a dimension label and at least one numeric measure. A
    # numeric date/header row has no nonnumeric first-column label and is skipped.
    start = next((r for r, row in enumerate(grid)
                  if row and row[0] and number(row[0]) is None
                  and any(number(value) is not None for value in row[1:])
                  and len(set(row)) > 1), None)
    if start is None or start == 0:
        return None
    headers = [" | ".join(dict.fromkeys(row[c] for row in grid[:start] if row[c]))
               for c in range(len(grid[0]))]
    rows, cells = [], []
    for source_row, row in enumerate(grid[start:], start + 1):
        if len(set(row)) == 1 or row[0].lower().startswith(("notes:", "note:")):
            continue
        if not any(number(value) is not None for value in row[1:]):
            continue
        rows.append({"source_row": source_row, "values": row})
        for column, token in enumerate(row[1:], 1):
            value = number(token)
            if value is None and token.casefold() not in _MISSING and not token.startswith("(Note"):
                continue
            # NBS housing places two city groups next to one another. The
            # nearest preceding City column belongs to this cell, not column 0.
            dimensions = [c for c in range(column) if re.search(r"\b(?:city|cities)\b", headers[c], re.I)]
            label_column = dimensions[-1] if dimensions else 0
            label = row[label_column]
            if not label or number(label) is not None:
                continue
            cells.append({"source_row": source_row, "source_column": column + 1,
                          "row_label": label, "column_label": headers[column],
                          "raw_value": token, "value": value,
                          "status": "observed" if value is not None else "unavailable"})
    if not cells:
        return None
    return {"table_id": f"table-{ordinal}", "context": table_context(table),
            "columns": headers, "rows": rows, "cells": cells,
            "table_sha256": digest(grid)}


def parse_release(raw: bytes, *, url: str, collected_at: str) -> dict:
    source_policy(url)
    soup = soup_from(raw)
    title_meta, date_meta = soup.find("meta", attrs={"name": "ArticleTitle"}), soup.find("meta", attrs={"name": "PubDate"})
    if not title_meta or not date_meta:
        raise NBSReleaseError("release title or publisher timestamp is missing")
    title = clean(title_meta.get("content", ""))
    family = classify(title)
    if family is None:
        raise NBSReleaseError("release is outside the reviewed economic families")
    try:
        released = datetime.strptime(clean(date_meta["content"]), "%Y/%m/%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        collected = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except (ValueError, KeyError) as exc:
        raise NBSReleaseError("invalid publisher or capture clock") from exc
    if collected.tzinfo is None or released > collected:
        raise NBSReleaseError("publisher timestamp is later than collection")
    tables, seen = [], set()
    for ordinal, table in enumerate(soup.find_all("table"), 1):
        if table.find_parent("table") is not None:
            continue
        parsed = extract_table(table, ordinal)
        if parsed and parsed["table_sha256"] not in seen:
            seen.add(parsed["table_sha256"])
            tables.append(parsed)
    if not tables:
        raise NBSReleaseError("no unambiguous statistical tables; manual review required")
    raw_hash = hashlib.sha256(raw).hexdigest()
    narrative_metrics = []
    if family == "pmi":
        text = clean(soup.get_text(" ", strip=True))
        for size, phrase in (("large", "large enterprises"), ("medium", "medium-sized enterprises"), ("small", "small-sized enterprises")):
            match = re.search(r"PMI for " + re.escape(phrase) + r" was (\d+(?:\.\d+)?)%", text, re.I)
            if match:
                value = float(match[1])
                if not 0 <= value <= 100:
                    raise NBSReleaseError("firm-size PMI outside 0..100")
                narrative_metrics.append({"metric": "manufacturing_pmi", "firm_size": size,
                                          "value": value, "unit": "diffusion index", "source_locator": "By enterprise size paragraph"})
    return {"release_id": digest({"url": url, "raw_sha256": raw_hash, "parser_version": PARSER_VERSION}),
            "family": family, "title": title, "source_url": url,
            "publisher": "National Bureau of Statistics of China",
            "independence_group": "nbs_official_statistics",
            "released_at": released.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "publisher_time_zone": "Asia/Shanghai", "collected_at": collected_at,
            "raw_sha256": raw_hash, "raw_bytes": len(raw), "parser_version": PARSER_VERSION,
            "measurement_scope": "source_table_cells_with_original_headings",
            "rights": {"status": "attributed_statistical_data", "terms_url": TERMS_URL,
                       "attribution": "Quoted from the website of the National Bureau of Statistics (www.stats.gov.cn)",
                       "license": "NBS statistical-data terms; no downstream sublicense"},
            "tables": tables, "narrative_metrics": narrative_metrics,
            "numeric_cells": sum(cell["value"] is not None for t in tables for cell in t["cells"]),
            "missing_cells": sum(cell["value"] is None for t in tables for cell in t["cells"])}
