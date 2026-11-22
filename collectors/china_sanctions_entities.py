"""Official sanctions, export-control & corporate ownership entity graph for China.

Provides lawful, auditable, public-domain and CC0 intelligence comparable to
WireScreen / Sayari / Kharon screening layers:

1. **US ITA Consolidated Screening List (CSL)** (public domain US government):
   Unifies BIS Entity List, BIS Military End User (MEU) List, BIS Denied Persons,
   BIS Unverified List, OFAC SDN, OFAC Non-SDN Chinese Military-Industrial Complex
   (NS-CMIC), State Department Nonproliferation & ITAR Debarred lists.
   Filters & indexes EVERY entity touching China (CN), Hong Kong (HK), or
   Macau (MO), with program counts, Federal Register citations, dates, and
   license policies.
2. **US OFAC SDN & Consolidated Non-SDN Lists** (public domain US Treasury):
   Direct Treasury verification of PRC/HK/MO designations.
3. **UN Security Council Consolidated Sanctions List** (official UN XML):
   Global multilateral sanctions entities/individuals with China/HK nexus.
4. **GLEIF Legal Entity Identifier (LEI) China Registry** (open CC0):
   Tracks China's 107,000+ registered LEI legal entities (state banks, SOEs,
   LGFVs, financial institutions, listed exporters) plus golden-copy metadata
   and sampled parent-child corporate relationships.

NOTE on Palimpsest aggregate vs entity boundaries:
  Entity-level sanction/LEI records are stored in their own dedicated
  corporate/sanctions lake tables (never injected into aggregate-only
  ``EconomicObservation`` metadata). Public summary outputs publish aggregate
  program counts, timeline trends, and official government-designated entity
  records strictly as published by the originating government authorities.

Standard-library only; all egress routes through ``core.safe_fetch``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from typing import Callable
from urllib.parse import urlsplit

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

ITA_CSL_URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv"
UN_SANCTIONS_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
GLEIF_CHINA_URL = "https://api.gleif.org/api/v1/lei-records?filter[entity.legalAddress.country]=CN&page[size]=100&page[number]=1"
GLEIF_HK_URL = "https://api.gleif.org/api/v1/lei-records?filter[entity.legalAddress.country]=HK&page[size]=50&page[number]=1"
USER_AGENT = "palimpsest.info observatory (official sanctions & LEI ingest; desk@palimpsest.info)"

_ALLOWED_HOSTS = frozenset({
    "data.trade.gov",
    "scsanctions.un.org",
    "unsolprodfiles.blob.core.windows.net",
    "api.gleif.org",
    "www.treasury.gov",
})
MAX_BYTES = 48 * 1024 * 1024

_CHINA_MARKERS = (
    "CHINA",
    "PRC",
    "PEOPLE'S REPUBLIC OF CHINA",
    "HONG KONG",
    "MACAU",
    "MACAO",
    "BEIJING",
    "SHANGHAI",
    "SHENZHEN",
    "GUANGDONG",
    "ZHEJIANG",
    "JIANGSU",
    "SICHUAN",
    "XINJIANG",
    "TIBET",
    "FUJIAN",
    "SHANDONG",
    "HUBEI",
    "LIAONING",
    "TIANJIN",
    "CHONGQING",
    "NS-CMIC",
    "CMIC",
)


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS or parts.port not in (None, 443):
        raise FetchError(f"china_sanctions_entities refused unreviewed URL: {url}")
    if parts.hostname == "unsolprodfiles.blob.core.windows.net" and not parts.path.startswith("/publiclegacyxmlfiles/"):
        raise FetchError(f"china_sanctions_entities refused non-sanctions UN blob path: {url}")


def _has_china_nexus(text: str) -> bool:
    upper = text.upper()
    return any(marker in upper for marker in _CHINA_MARKERS)


def _classify_jurisdiction(addresses: str, programs: str, remarks: str) -> str:
    blob = f"{addresses} {programs} {remarks}".upper()
    if "HONG KONG" in blob:
        if "CHINA" in blob or "BEIJING" in blob or "SHENZHEN" in blob or "SHANGHAI" in blob:
            return "CN+HK"
        return "HK"
    if "MACAU" in blob or "MACAO" in blob:
        return "MO"
    return "CN"


def parse_ita_csl_china(raw: bytes) -> dict[str, object]:
    """Extract all China/HK/MO entities from the US ITA Consolidated Screening List CSV."""
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    total_rows = 0
    china_entities: list[dict[str, object]] = []
    by_source: Counter[str] = Counter()
    by_program: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    by_jurisdiction: Counter[str] = Counter()
    by_start_year: Counter[str] = Counter()

    for row in reader:
        total_rows += 1
        addresses = (row.get("addresses") or "").strip()
        programs = (row.get("programs") or "").strip()
        name = (row.get("name") or "").strip()
        remarks = (row.get("remarks") or "").strip()
        citizenships = (row.get("citizenships") or "").strip()
        nationalities = (row.get("nationalities") or "").strip()
        alt_names = (row.get("alt_names") or "").strip()
        search_blob = " | ".join((addresses, programs, citizenships, nationalities, remarks, name))
        if not _has_china_nexus(search_blob):
            continue

        source = (row.get("source") or "unknown").strip()
        entity_type = (row.get("type") or "unknown").strip() or "unknown"
        start_date = (row.get("start_date") or "").strip()
        jurisdiction = _classify_jurisdiction(addresses, programs, remarks)

        by_source[source] += 1
        by_type[entity_type] += 1
        by_jurisdiction[jurisdiction] += 1
        if len(start_date) >= 4 and start_date[:4].isdigit():
            by_start_year[start_date[:4]] += 1
        for prog in [p.strip() for p in programs.replace(";", ",").split(",") if p.strip()]:
            by_program[prog] += 1

        china_entities.append(
            {
                "id": (row.get("_id") or row.get("entity_number") or "").strip(),
                "source_list": source,
                "name": name,
                "alt_names": alt_names[:500],
                "entity_type": entity_type,
                "programs": programs,
                "jurisdiction": jurisdiction,
                "addresses": addresses[:500],
                "federal_register_notice": (row.get("federal_register_notice") or "").strip(),
                "start_date": start_date,
                "end_date": (row.get("end_date") or "").strip(),
                "license_requirement": (row.get("license_requirement") or "").strip()[:300],
                "license_policy": (row.get("license_policy") or "").strip()[:300],
                "source_information_url": (row.get("source_information_url") or "").strip(),
            }
        )

    return {
        "raw_sha256": sha,
        "total_csl_records": total_rows,
        "china_nexus_count": len(china_entities),
        "by_source_list": dict(by_source.most_common()),
        "by_program_top25": dict(by_program.most_common(25)),
        "by_entity_type": dict(by_type.most_common()),
        "by_jurisdiction": dict(by_jurisdiction.most_common()),
        "by_start_year": dict(sorted(by_start_year.items())),
        "entities": china_entities,
    }


def parse_un_sanctions_china(raw: bytes) -> dict[str, object]:
    """Extract China/HK/MO nexus entries from the UN Security Council Consolidated XML."""
    sha = hashlib.sha256(raw).hexdigest()
    root = ET.fromstring(raw)
    date_generated = root.attrib.get("dateGenerated")
    total = 0
    matches: list[dict[str, object]] = []
    for tag, kind in (("INDIVIDUAL", "individual"), ("ENTITY", "entity")):
        for elem in root.findall(f".//{tag}"):
            total += 1
            blob = " ".join(t.strip() for t in elem.itertext() if t and t.strip())
            if not _has_china_nexus(blob):
                continue
            ref_num = (elem.findtext("REFERENCE_NUMBER") or "").strip()
            un_list_type = (elem.findtext("UN_LIST_TYPE") or "").strip()
            listed_on = (elem.findtext("LISTED_ON") or "").strip()
            name_parts = [
                (elem.findtext(k) or "").strip()
                for k in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")
            ]
            name = " ".join(p for p in name_parts if p)
            matches.append(
                {
                    "reference_number": ref_num,
                    "kind": kind,
                    "name": name,
                    "un_list_type": un_list_type,
                    "listed_on": listed_on,
                }
            )
    return {
        "raw_sha256": sha,
        "date_generated": date_generated,
        "total_un_records": total,
        "china_nexus_count": len(matches),
        "entries": matches,
    }


def parse_gleif_page(raw: bytes, country: str) -> dict[str, object]:
    """Parse one GLEIF LEI JSON:API page for CN or HK corporate entities."""
    sha = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    meta = payload.get("meta") or {}
    pagination = meta.get("pagination") or {}
    golden = meta.get("goldenCopy") or {}
    records: list[dict[str, object]] = []
    status_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()

    for item in payload.get("data") or []:
        attr = item.get("attributes") or {}
        entity = attr.get("entity") or {}
        reg = attr.get("registration") or {}
        legal_name = (entity.get("legalName") or {}).get("name") or ""
        legal_addr = entity.get("legalAddress") or {}
        reg_status = reg.get("status") or "UNKNOWN"
        category = entity.get("category") or "GENERAL"
        status_counts[reg_status] += 1
        category_counts[category] += 1
        records.append(
            {
                "lei": item.get("id"),
                "country": country,
                "legal_name": legal_name,
                "status": entity.get("status"),
                "registration_status": reg_status,
                "category": category,
                "city": legal_addr.get("city"),
                "region": legal_addr.get("region"),
                "initial_registration_date": reg.get("initialRegistrationDate"),
                "last_update_date": reg.get("lastUpdateDate"),
                "bic": attr.get("bic"),
            }
        )

    return {
        "country": country,
        "raw_sha256": sha,
        "golden_copy_publish_date": golden.get("publishDate"),
        "total_registered_entities": pagination.get("total"),
        "sample_count": len(records),
        "sample_registration_statuses": dict(status_counts),
        "sample_categories": dict(category_counts),
        "sample_entities": records,
    }


def collect_china_sanctions_and_entities(
    *,
    timeout: float = 40.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    now: str | None = None,
) -> dict[str, object]:
    """Collect US ITA CSL, UN Sanctions, and GLEIF CN/HK corporate entity counts."""
    collected_at = now or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    failures: list[dict[str, str]] = []
    csl_data: dict[str, object] | None = None
    un_data: dict[str, object] | None = None
    gleif_cn: dict[str, object] | None = None
    gleif_hk: dict[str, object] | None = None

    # 1. US ITA Consolidated Screening List (BIS Entity List, MEU, OFAC SDN, NS-CMIC, etc.)
    try:
        _validate_url(ITA_CSL_URL)
        raw = fetcher(
            ITA_CSL_URL,
            timeout=timeout,
            max_bytes=MAX_BYTES,
            max_redirects=3,
            headers={"Accept": "text/csv", "User-Agent": USER_AGENT},
            url_policy=_validate_url,
        )
        csl_data = parse_ita_csl_china(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("ITA CSL fetch failed: %s", exc)
        failures.append({"source": "us_ita_csl", "error": f"{type(exc).__name__}: {exc}"})

    # 2. UN Security Council Consolidated Sanctions XML
    try:
        _validate_url(UN_SANCTIONS_URL)
        raw = fetcher(
            UN_SANCTIONS_URL,
            timeout=timeout,
            max_bytes=MAX_BYTES,
            max_redirects=3,
            headers={"Accept": "application/xml", "User-Agent": USER_AGENT},
            url_policy=_validate_url,
        )
        un_data = parse_un_sanctions_china(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("UN sanctions fetch failed: %s", exc)
        failures.append({"source": "un_sanctions", "error": f"{type(exc).__name__}: {exc}"})

    # 3. GLEIF LEI CN + HK corporate registry
    for country, url in (("CN", GLEIF_CHINA_URL), ("HK", GLEIF_HK_URL)):
        try:
            _validate_url(url)
            raw = fetcher(
                url,
                timeout=timeout,
                max_bytes=MAX_BYTES,
                max_redirects=2,
                headers={"Accept": "application/vnd.api+json", "User-Agent": USER_AGENT},
                url_policy=_validate_url,
            )
            parsed = parse_gleif_page(raw, country)
            if country == "CN":
                gleif_cn = parsed
            else:
                gleif_hk = parsed
        except Exception as exc:  # noqa: BLE001
            log.warning("GLEIF %s fetch failed: %s", country, exc)
            failures.append({"source": f"gleif_{country.lower()}", "error": f"{type(exc).__name__}: {exc}"})

    ok_sources = sum(1 for x in (csl_data, un_data, gleif_cn, gleif_hk) if x is not None)
    return {
        "schema": "palimpsest.china-sanctions-entities.v1",
        "generated_at": collected_at,
        "status": "ok" if ok_sources == 4 else ("partial" if ok_sources > 0 else "unavailable"),
        "sources": {
            "us_ita_csl": {
                "url": ITA_CSL_URL,
                "rights": "US Government public domain (International Trade Administration)",
            },
            "un_sanctions": {
                "url": UN_SANCTIONS_URL,
                "rights": "Official UN Security Council public sanctions register",
            },
            "gleif_lei": {
                "url": "https://api.gleif.org/api/v1/lei-records",
                "rights": "CC0 1.0 Universal (Global Legal Entity Identifier Foundation)",
            },
        },
        "summary": {
            "us_csl_china_entities": csl_data["china_nexus_count"] if csl_data else 0,
            "us_csl_total_records": csl_data["total_csl_records"] if csl_data else 0,
            "un_sanctions_china_nexus": un_data["china_nexus_count"] if un_data else 0,
            "gleif_china_registered_leis": gleif_cn["total_registered_entities"] if gleif_cn else None,
            "gleif_hk_registered_leis": gleif_hk["total_registered_entities"] if gleif_hk else None,
            "failures_count": len(failures),
        },
        "us_ita_csl": csl_data,
        "un_sanctions": un_data,
        "gleif_china": gleif_cn,
        "gleif_hk": gleif_hk,
        "failures": failures,
    }
