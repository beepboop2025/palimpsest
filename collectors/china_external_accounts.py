"""Bounded SAFE workbook acquisition and private statistical normalization.

The public source's publication permission is NOT an open licence. This module
does not authorize public redistribution; only collector metadata may be exposed.
"""
from __future__ import annotations

import calendar
import hashlib
import io
import json
import math
import posixpath
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from core.safe_fetch import safe_fetch_bytes

ROOT = Path(__file__).resolve().parents[1]
PARSER_VERSION = "safe-external-accounts.v1"
SCHEMA = "palimpsest.china-external-accounts.v1"
MAX_BYTES = 4 * 1024 * 1024
MAX_EXPANDED = 48 * 1024 * 1024
MAX_CELLS = 400_000
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SOURCE_PAGES = frozenset({"/en/2019/0329/1496.html", "/en/2019/0919/1561.html"})
MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}


def digest(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def load_registry() -> dict:
    return json.loads((ROOT / "config/china_external_accounts.json").read_text())


def source_policy(url: str) -> None:
    p = urlsplit(url)
    if (p.scheme != "https" or p.netloc != "www.safe.gov.cn" or p.query or p.fragment
            or (p.path not in SOURCE_PAGES and not re.fullmatch(r"/en/file/file/\d{8}/[a-f0-9]{32}\.xlsx", p.path))):
        raise ValueError("SAFE source URL is outside the reviewed origin and paths")


def fetch(url: str) -> bytes:
    source_policy(url)
    return safe_fetch_bytes(url, max_bytes=MAX_BYTES, timeout=35, max_redirects=0, url_policy=source_policy)


def discover(raw: bytes, source: dict, *, now: str) -> list[dict]:
    source_policy(source["url"])
    soup = BeautifulSoup(raw, "html.parser")
    # SAFE preserves a stable page URL while replacing the workbook attachment.
    # The page's Dispatch date describes its current edition, not every old file.
    result = []
    for a in soup.select("a[href]"):
        title = a.get_text(" ", strip=True)
        url = urljoin(source["url"], a["href"])
        if not title or not url.endswith(".xlsx"):
            continue
        if source["kind"] == "regional" and not re.search(r"in (20\d{2})\s*\(by Region\)", title):
            continue
        if source["kind"] == "bop" and "time-series data of balance of payments" not in title.lower():
            continue
        source_policy(url)
        file_date = re.search(r"/file/(\d{8})/", url).group(1)
        released = datetime.strptime(file_date, "%Y%m%d").date().isoformat()
        if released > datetime.fromisoformat(now.replace("Z", "+00:00")).date().isoformat():
            raise ValueError("SAFE attachment path has a future date")
        result.append({"source_id": source["id"], "kind": source["kind"], "source_page": source["url"],
                       "url": url, "title": title, "attachment_date": released,
                       "date_basis": "attachment URL date; not an independently verified publication timestamp"})
    return sorted({x["url"]: x for x in result}.values(), key=lambda x: x["url"], reverse=True)[:12]


def _xml(raw: bytes):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("workbook XML declarations are forbidden")
    return ET.fromstring(raw)


def _coord(ref: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Z]{1,3})([1-9]\d{0,5})", ref)
    if not m:
        raise ValueError("invalid workbook cell coordinate")
    col = 0
    for c in m[1]:
        col = col * 26 + ord(c) - 64
    row = int(m[2])
    if row > 5000 or col > 180:
        raise ValueError("workbook grid exceeds limits")
    return row, col


def read_workbook(raw: bytes) -> list[dict]:
    """Read stored values without executing formulas, macros or external links."""
    if len(raw) > MAX_BYTES:
        raise ValueError("workbook byte limit exceeded")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(infos) > 500 or len(set(names)) != len(names):
            raise ValueError("workbook ZIP member count or duplicate names")
        if sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError("workbook expansion limit exceeded")
        for i in infos:
            if (i.filename.startswith("/") or ".." in i.filename.split("/") or "\\" in i.filename
                    or i.flag_bits & 1 or i.file_size > 24 * 1024 * 1024):
                raise ValueError("unsafe workbook ZIP member")
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = ["".join(t.text or "" for t in item.findall(".//s:t", NS))
                      for item in _xml(archive.read("xl/sharedStrings.xml"))]
            if len(shared) > 100_000 or any(len(s) > 20_000 for s in shared):
                raise ValueError("workbook string limits exceeded")
        rels = {}
        for rel in _xml(archive.read("xl/_rels/workbook.xml.rels")):
            if rel.get("TargetMode") == "External":
                raise ValueError("external workbook relationship")
            target = rel.get("Target", "")
            normalized = posixpath.normpath(posixpath.join("xl", target)) if not target.startswith("/") else target.lstrip("/")
            if not normalized.startswith("xl/") or normalized not in names:
                raise ValueError("workbook relationship escapes archive")
            rels[rel.get("Id")] = normalized
        sheets = _xml(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS)
        if not 1 <= len(sheets) <= 24:
            raise ValueError("workbook sheet count exceeds limits")
        result, seen_cells = [], 0
        for sh in sheets:
            if sh.get("state", "visible") != "visible":
                continue
            target = rels[sh.get("{" + REL_NS + "}id")]
            if not target.startswith("xl/worksheets/"):
                raise ValueError("sheet relationship is not a worksheet")
            xml = _xml(archive.read(target))
            grid, formula_cells = {}, set()
            for cell in xml.findall(".//s:sheetData/s:row/s:c", NS):
                seen_cells += 1
                if seen_cells > MAX_CELLS:
                    raise ValueError("workbook cell limit exceeded")
                coord = _coord(cell.get("r", ""))
                if coord in grid:
                    raise ValueError("duplicate workbook coordinate")
                v = cell.find("s:v", NS)
                typ = cell.get("t", "n")
                token = v.text or "" if v is not None else ""
                if typ == "s" and token:
                    idx = int(token)
                    if not 0 <= idx < len(shared):
                        raise ValueError("invalid shared-string index")
                    token = shared[idx]
                elif typ == "inlineStr":
                    token = "".join(t.text or "" for t in cell.findall("s:is//s:t", NS))
                if cell.find("s:f", NS) is not None:
                    formula_cells.add(coord)
                grid[coord] = token.strip()
            merges = []
            for merge in xml.findall("s:mergeCells/s:mergeCell", NS):
                bounds = merge.get("ref", "").split(":")
                if len(bounds) != 2:
                    raise ValueError("invalid merged cell range")
                start, end = _coord(bounds[0]), _coord(bounds[1])
                if end[0] < start[0] or end[1] < start[1] or (end[0] - start[0] + 1) * (end[1] - start[1] + 1) > 10_000:
                    raise ValueError("merged cell range exceeds limits")
                merges.append((start, end))
            result.append({"name": sh.get("name", ""), "grid": grid, "merges": merges, "formula_cells": formula_cells})
        return result


def number(token: str) -> float | None:
    if not token or token in {"--", "-", "…", "...", "..", "—", "N/A"}:
        return None
    try:
        value = float(token.replace(",", "").replace("−", "-"))
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def _header(sheet: dict, row: int, col: int) -> str:
    value = sheet["grid"].get((row, col), "")
    if value:
        return value
    for start, end in sheet["merges"]:
        if start[0] <= row <= end[0] and start[1] <= col <= end[1]:
            return sheet["grid"].get(start, "")
    return ""


def parse_workbook(raw: bytes, *, item: dict, collected_at: str) -> dict:
    source_policy(item["url"])
    clock = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    if clock.tzinfo is None or item["attachment_date"] > clock.date().isoformat():
        raise ValueError("invalid SAFE evidence clocks")
    raw_hash = digest(raw)
    capture_id = digest({"url": item["url"], "raw_sha256": raw_hash, "parser_version": PARSER_VERSION})
    observations, sheet_summaries = [], []
    try:
        workbook = read_workbook(raw)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError) as exc:
        raise ValueError("invalid SAFE workbook structure") from exc
    for sheet in workbook:
        g = sheet["grid"]
        titles = " ".join(g.get((r, 1), "") for r in range(1, 4))
        if item["kind"] == "regional":
            # The November 2023 source title omits the space before its year.
            date = re.search(r"in\s+([A-Za-z]+)\s*(20\d{2})\s*\(by Region\)", titles, re.I)
            if not date or date[1].lower() not in MONTHS or "USD 100 million" not in titles:
                raise ValueError("unrecognized SAFE regional period or unit")
            if sheet["name"].lower() != date[1].lower():
                raise ValueError("SAFE regional sheet name and title month differ")
            period = f"{date[2]}-{MONTHS[date[1].lower()]:02d}"
            if period > collected_at[:7]:
                raise ValueError("future SAFE observation period")
            unit, frequency = "USD 100 million", "monthly"
            cols = [(c, _header(sheet, 3, c)) for c in range(2, 181) if _header(sheet, 3, c)]
            if len(cols) != 36 or len({label for _, label in cols}) != 36:
                raise ValueError("SAFE regional coverage must have 36 distinct reporting areas")
            note = " ".join(g.get((r, 1), "") for r in range(34, 41)).lower()
            if not all(term in note for term in ("liaoning excludes dalian", "zhejiang excludes ningbo", "fujian excludes xiamen", "shandong excludes qingdao", "guangdong excludes shenzhen")):
                raise ValueError("SAFE regional exclusions are not verified; aggregation would be unsafe")
            rows = []
            flow = ""
            for r in range(4, 34):
                label = g.get((r, 1), "")
                if label.startswith("I."): flow = "receipts"
                elif label.startswith("II."): flow = "payments"
                elif label.startswith("III."): flow = "balance"
                if not label or "By transaction" in label:
                    continue
                metric = "total" if re.match(r"I{1,3}\.", label) else re.sub(r"^(?:[\d.]+\s*|of which:\s*)", "", label).strip().lower()
                rows.append((r, flow + ":" + metric, label, flow))
            for r, metric, label, flow in rows:
                for c, region in cols:
                    observations.append(_observation(item["kind"], sheet, r, c, region, metric, label, period, frequency, unit, flow))
        else:
            if "Balance of Payments" not in titles:
                raise ValueError("unrecognized SAFE BOP sheet")
            unit_text = g.get((3, 1), "")
            unit = next((v for term, v in (("US dollars", "USD 100 million"), ("Renminbi", "CNY 100 million"), ("SDR", "SDR 100 million")) if term in unit_text), None)
            if unit is None:
                raise ValueError("unrecognized SAFE BOP unit")
            frequency = "quarterly" if "quarterly" in titles else "annual"
            cols = [(c, _header(sheet, 4, c)) for c in range(2, 181) if re.fullmatch(r"(?:19|20)\d{2}(?:Q[1-4])?", _header(sheet, 4, c))]
            if not cols:
                raise ValueError("SAFE BOP has no recognized period columns")
            parent = ""
            for r in range(6, 400):
                label = g.get((r, 1), "")
                if label.startswith("Notes:"):
                    break
                if not label:
                    continue
                if re.match(r"^\d", label): parent = label
                elif label not in {"Credit", "Debit"}: continue
                metric = parent + (" | " + label if label in {"Credit", "Debit"} else "")
                flow = label.lower() if label in {"Credit", "Debit"} else "reported_balance"
                for c, period in cols:
                    if int(period[:4]) > clock.year or ("Q" in period and int(period[:4]) == clock.year and int(period[-1]) > (clock.month - 1) // 3 + 1):
                        raise ValueError("future SAFE BOP observation year")
                    observations.append(_observation(item["kind"], sheet, r, c, "China", metric, label, period, frequency, unit, flow))
        sr = [o for o in observations if o["sheet"] == sheet["name"]]
        sheet_summaries.append({"sheet": sheet["name"], "unit": unit, "frequency": frequency,
                               "observations": len(sr), "numeric_observations": sum(o["value"] is not None for o in sr),
                               "period_start": min(o["period"] for o in sr), "period_end": max(o["period"] for o in sr)})
    if not observations or not any(o["value"] is not None for o in observations):
        raise ValueError("SAFE workbook has no parsed numeric observations")
    return {"capture_id": capture_id, **item, "collected_at": collected_at,
            "raw_sha256": raw_hash, "raw_bytes": len(raw), "parser_version": PARSER_VERSION,
            "numeric_observations": sum(o["value"] is not None for o in observations),
            "unavailable_observations": sum(o["value"] is None for o in observations),
            "geography_basis": "reporting bank location; five separately listed cities excluded from their provinces" if item["kind"] == "regional" else "national external accounts",
            "sheets": sheet_summaries, "observations": observations}


def _observation(kind, sheet, row, col, region, metric, label, period, frequency, unit, flow):
    raw_value = sheet["grid"].get((row, col), "")
    formula = (row, col) in sheet["formula_cells"]
    value = None if formula else number(raw_value)
    key = {"kind": kind, "region": region, "metric": metric, "period": period, "unit": unit}
    return {"observation_key": digest(key), **key, "sheet": sheet["name"], "source_row": row,
            "source_column": col, "row_label": label, "frequency": frequency, "flow": flow,
            "raw_value": raw_value, "value": value,
            "status": "formula_not_evaluated" if formula else "observed" if value is not None else "unavailable"}
