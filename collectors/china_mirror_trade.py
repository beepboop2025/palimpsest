"""Bounded Eurostat EU-declared merchandise trade, independently reported of China.

Only EU reporters and HS2/HS4 aggregates are admitted. This excludes the
third-country reporter and Austrian CN8 exceptions in Eurostat's reuse terms.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import urllib.parse
from datetime import datetime, timezone

from core.safe_fetch import safe_fetch_bytes

API_URL = "https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/DS-045409"
DATASET_URL = "https://ec.europa.eu/eurostat/databrowser/view/DS-045409/default/table?lang=en"
TERMS_URL = "https://ec.europa.eu/eurostat/help/copyright-notice"
SOURCE_GROUP = "eurostat_eu_reported_trade"
PARSER_VERSION = "eurostat-eu-mirror.v1"
REPORTERS = {"EU27_2020", "DE", "FR", "NL", "IT", "ES", "PL", "BE", "CZ", "SE"}
PARTNERS = {"CN": "China", "PK": "Pakistan", "MM": "Myanmar"}
INDICATORS = {"VALUE_IN_EUROS": "value_eur", "QUANTITY_IN_100KG": "weight_kg"}
FLOWS = {"1": "eu_imports_from_partner", "2": "eu_exports_to_partner"}
RIGHTS = {"status": "attributed_eu_declared_statistical_data", "terms_url": TERMS_URL,
          "license": "Eurostat statistical-data reuse terms; no downstream sublicense",
          "attribution": "Source: Eurostat, DS-045409; EU-reported trade. Palimpsest calculations and presentation; Eurostat is not responsible for these modifications."}
MAX_BYTES = 12 * 1024 * 1024
MAX_CELLS = 200_000


def digest(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def clock(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def valid_product(value: str) -> bool:
    return value == "TOTAL" or bool(re.fullmatch(r"(?:0[1-9]|[1-9][0-9])(?:[0-9]{2})?", value))


def source_policy(url: str) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "ec.europa.eu" or parsed.fragment
            or parsed.path != urllib.parse.urlsplit(API_URL).path):
        raise ValueError("unapproved Eurostat source")
    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    if set(query) != {"lang", "freq", "reporter", "partner", "product", "flow", "indicators", "sinceTimePeriod"}:
        raise ValueError("unexpected Eurostat query fields")
    if query["lang"] != ["EN"] or query["freq"] != ["M"] or query["flow"] != ["1", "2"] or query["indicators"] != list(INDICATORS):
        raise ValueError("unsupported Eurostat dimensions")
    if len(query["reporter"]) != 1 or query["reporter"][0] not in REPORTERS or len(query["partner"]) != 1 or query["partner"][0] not in PARTNERS:
        raise ValueError("only EU-reported partner trade is admitted")
    if not 1 <= len(query["product"]) <= 30 or len(set(query["product"])) != len(query["product"]) or not all(valid_product(x) for x in query["product"]):
        raise ValueError("only bounded HS2/HS4 products admitted")
    if len(query["sinceTimePeriod"]) != 1 or not re.fullmatch(r"20[0-9]{2}-(?:0[1-9]|1[0-2])", query["sinceTimePeriod"][0]):
        raise ValueError("invalid starting month")
    return query


def build_url(reporter: str, partner: str, products: list[str], start: str = "2010-01") -> str:
    params = [("lang", "EN"), ("freq", "M"), ("reporter", reporter), ("partner", partner)]
    params += [("product", product) for product in products]
    params += [("flow", x) for x in FLOWS] + [("indicators", x) for x in INDICATORS] + [("sinceTimePeriod", start)]
    result = API_URL + "?" + urllib.parse.urlencode(params)
    source_policy(result)
    return result


def fetch_bytes(url: str) -> bytes:
    source_policy(url)
    return safe_fetch_bytes(url, max_bytes=MAX_BYTES, timeout=45, max_redirects=0,
                            url_policy=source_policy,
                            headers={"User-Agent": "Palimpsest-Economic-Research/1.0 (+https://www.palimpsest.info/)", "Accept": "application/json"})


def parse_response(raw: bytes, *, source_url: str, collected_at: str) -> tuple[dict, list[dict]]:
    query = source_policy(source_url)
    collected = clock(collected_at)
    if len(raw) > MAX_BYTES:
        raise ValueError("Eurostat response exceeds bound")
    doc = json.loads(raw)
    names = ["freq", "reporter", "partner", "product", "flow", "indicators", "time"]
    if doc.get("class") != "dataset" or doc.get("source") != "ESTAT" or doc.get("id") != names or doc.get("extension", {}).get("id") != "DS-045409":
        raise ValueError("Eurostat dataset identity mismatch or asynchronous response")
    updated = clock(doc["updated"])
    if updated > collected:
        raise ValueError("source update is in the future")
    sizes = doc["size"]
    if len(sizes) != len(names) or any(type(n) is not int or n <= 0 for n in sizes) or math.prod(sizes) > MAX_CELLS:
        raise ValueError("Eurostat cube exceeds bound")
    dimensions = []
    for name, size in zip(names, sizes):
        index = doc["dimension"][name]["category"]["index"]
        if not isinstance(index, dict) or sorted(index.values()) != list(range(size)):
            raise ValueError("invalid JSON-stat category positions")
        categories = sorted(index, key=index.get)
        if name != "time" and set(categories) != set(query[name]):
            raise ValueError("response dimensions differ from query")
        if name == "time" and any(not re.fullmatch(r"20[0-9]{2}-(?:0[1-9]|1[0-2])", x) or x < query["sinceTimePeriod"][0] for x in categories):
            raise ValueError("invalid response period")
        dimensions.append(categories)
    values, flags = doc.get("value", {}), doc.get("status", {})
    def sparse(mapping, position):
        return mapping.get(str(position)) if isinstance(mapping, dict) else mapping[position] if position < len(mapping) else None
    if not isinstance(values, (dict, list)) or not isinstance(flags, (dict, list)):
        raise ValueError("invalid JSON-stat values")
    count = math.prod(sizes)
    for mapping in (values, flags):
        if isinstance(mapping, dict) and any(not str(k).isdigit() or int(k) >= count for k in mapping):
            raise ValueError("JSON-stat cell outside cube")
        if isinstance(mapping, list) and len(mapping) > count:
            raise ValueError("JSON-stat cell outside cube")
    snapshot_id = digest({"source_url": source_url, "raw_sha256": digest(raw), "parser_version": PARSER_VERSION})
    rows = {}
    for position, coordinates in enumerate(itertools.product(*dimensions)):
        _, reporter, partner, product, flow, indicator, period = coordinates
        value, flag = sparse(values, position), sparse(flags, position)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("invalid trade value")
        if flag is not None and (not isinstance(flag, str) or len(flag) > 40):
            raise ValueError("invalid statistical flag")
        # JSON-stat includes future empty calendar slots. They are not observations.
        if period > collected.strftime("%Y-%m"):
            if value is not None:
                raise ValueError("future trade observation")
            continue
        key = (reporter, partner, product, flow, period)
        if key not in rows:
            rows[key] = {"reporter": reporter, "partner": partner, "product": product,
                         "product_label": doc["dimension"]["product"]["category"].get("label", {}).get(product, product),
                         "flow": FLOWS[flow], "period": period, "snapshot_id": snapshot_id,
                         "source_updated_at": updated.isoformat().replace("+00:00", "Z"), "collected_at": collected_at}
        metric = INDICATORS[indicator]
        rows[key][metric] = value * 100 if value is not None and metric == "weight_kg" else value
        rows[key][metric + "_status"] = "reported" if value is not None else "unavailable"
        rows[key][metric + "_flag"] = flag or ""
    result = sorted(rows.values(), key=lambda row: tuple(row[k] for k in ("reporter", "partner", "product", "flow", "period")))
    if not any(row["value_eur"] is not None for row in result):
        raise ValueError("no reported trade values")
    snapshot = {"snapshot_id": snapshot_id, "source_url": source_url, "raw_sha256": digest(raw), "raw_bytes": len(raw),
                "parser_version": PARSER_VERSION, "collected_at": collected_at,
                "source_updated_at": updated.isoformat().replace("+00:00", "Z"), "rows": len(result)}
    return snapshot, result
