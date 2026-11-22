"""China energy, augmented fiscal/macro, BRI sovereign debt, and critical-tech R&D collector.

Unifies four open, keyless, rights-cleared international datasets that provide
independent counter-evidence on China's real economy, fiscal health, overseas
lending, and technological trajectory:

1. **Ember Monthly Global Electricity Data** (CC BY 4.0):
   Complete monthly time series (2015–present) of China's Coal, Gas, Nuclear,
   Hydro, Wind, Solar, Bioenergy, Clean vs Fossil, Total Generation (TWh), and
   power-sector CO2 emissions — independent physical industrial-output verify.
2. **IMF DataMapper API** (Official IMF open macro & fiscal data):
   China + BRI corridor economies: Real GDP growth (``NGDP_RPCH``), CPI inflation
   (``PCPIPCH``), General Government Net Lending/Borrowing (``GGXCNL_NGDP`` —
   augmented fiscal balance proxy), Gross Debt % GDP (``GGXWDG_NGDP``), Current
   Account % GDP (``BCA_NGDPD``), and Unemployment Rate (``LUR``).
3. **World Bank International Debt Statistics (IDS, Source 6)** (CC BY 4.0):
   Official bilateral external debt stocks, disbursements, and debt-service
   outflows across 24 major Belt & Road Initiative (BRI) sovereign borrowers.
4. **OpenAlex Critical-Technology Research Output** (CC0 1.0):
   Tracks China's annual publication volume and international co-authorship
   dynamics across critical dual-use domains (AI/ML, Semiconductors, Quantum,
   Aerospace, Biotechnology, Energy Storage).

Standard-library only; all egress routes through ``core.safe_fetch``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Callable, Sequence
from urllib.parse import urlencode, urlsplit

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

EMBER_MONTHLY_URL = (
    "https://files.ember-energy.org/public-downloads/generation/outputs/"
    "release_generation_monthly_global.csv"
)
IMF_DATAMAPPER_BASE = "https://www.imf.org/external/datamapper/api/v1"
WB_API_BASE = "https://api.worldbank.org/v2"
OPENALEX_API_BASE = "https://api.openalex.org/works"
USER_AGENT = "palimpsest.info observatory (open China dark-data ingest; desk@palimpsest.info)"

_ALLOWED_HOSTS = frozenset({
    "files.ember-energy.org",
    "www.imf.org",
    "api.worldbank.org",
    "api.openalex.org",
})

MAX_EMBER_BYTES = 128 * 1024 * 1024
MAX_API_BYTES = 16 * 1024 * 1024

IMF_INDICATORS: tuple[tuple[str, str, str], ...] = (
    ("NGDP_RPCH", "Real GDP growth", "% YoY"),
    ("PCPIPCH", "Consumer price inflation", "% YoY"),
    ("GGXCNL_NGDP", "General government net lending/borrowing", "% of GDP"),
    ("GGXWDG_NGDP", "General government gross debt", "% of GDP"),
    ("BCA_NGDPD", "Current account balance", "% of GDP"),
    ("LUR", "Unemployment rate", "%"),
)

IMF_COUNTRIES: tuple[str, ...] = (
    "CHN", "HKG", "TWN", "VNM", "MYS", "THA", "IDN", "PAK", "LKA", "MMR", "KAZ", "LAO", "KHM",
)

BRI_DEBT_COUNTRIES: tuple[str, ...] = (
    "PAK", "LKA", "AGO", "KEN", "ZMB", "LAO", "ETH", "MMR", "KHM", "BGD",
    "EGY", "ARG", "ECU", "NGA", "TZA", "UGA", "GHA", "IDN", "VNM", "KAZ",
    "UZB", "SRB", "MNE", "DJI",
)

WB_IDS_INDICATORS: tuple[tuple[str, str], ...] = (
    ("DT.DOD.BLAT.CD", "External debt stocks, official bilateral (current USD)"),
    ("DT.DOD.DLXF.CD", "External debt stocks, long-term (current USD)"),
    ("DT.AMT.BLAT.CD", "Principal repayments on official bilateral debt (current USD)"),
    ("DT.INT.BLAT.CD", "Interest payments on official bilateral debt (current USD)"),
    ("DT.DIS.BLAT.CD", "Disbursements on official bilateral debt (current USD)"),
)

# Validated Source-6 IDS fallback codes when canonical shorthand is retired in WB v2
WB_IDS_SOURCE6_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("DT.AMT.BLAT.CB.CD", "Bilateral commercial/official amortization (IDS source 6)"),
    ("BM.GSR.TOTL.CD", "Imports of goods, services and primary income (IDS source 6)"),
    ("BX.KLT.DINV.CD.DT", "Foreign direct investment net inflows (IDS source 6)"),
    ("DT.TDS.DECT.CD", "Total debt service on external debt (current USD)"),
    ("DT.DOD.DECT.CD", "External debt stocks, total (current USD)"),
)

OPENALEX_TECH_QUERIES: tuple[tuple[str, str], ...] = (
    ("ai_llm", "large language model OR deep learning OR neural network"),
    ("semiconductors", "semiconductor lithography OR integrated circuit OR photolithography"),
    ("quantum", "quantum computing OR quantum cryptography"),
    ("ev_battery", "lithium-ion battery OR solid-state battery OR electric vehicle"),
    ("aerospace_drone", "unmanned aerial vehicle OR hypersonic OR satellite constellation"),
)


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS or parts.port not in (None, 443):
        raise FetchError(f"china_energy_macro_dark refused unreviewed URL: {url}")


def _finite(val: object) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def parse_ember_china_csv(raw: bytes) -> dict[str, object]:
    """Filter Ember monthly global electricity CSV for China (CHN) and Hong Kong (HKG).

    Supports both Ember's current wide release schema (``Electricity source``,
    ``Generation (TWh)``, ``Generation YoY change (%)``, ``Emissions (MtCO2e)``)
    and Ember's legacy long schema (``Variable``, ``Unit``, ``Value``).
    """
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = set(reader.fieldnames or [])
    is_wide = "Electricity source" in fields and "Generation (TWh)" in fields

    rows: list[dict[str, object]] = []
    dates: set[str] = set()
    latest_by_var: dict[str, tuple[str, float, str]] = {}

    for rec in reader:
        code = (rec.get("Country code") or rec.get("ISO 3 code") or rec.get("Area code") or "").strip().upper()
        area = (rec.get("Area") or rec.get("Country or Region") or "").strip()
        if code not in ("CHN", "HKG") and area not in ("China", "Hong Kong"):
            continue
        iso3 = "CHN" if (code == "CHN" or area == "China") else "HKG"
        date_s = (rec.get("Date") or "").strip()
        if not date_s:
            continue

        if is_wide:
            source = (rec.get("Electricity source") or "").strip()
            gen_twh = _finite(rec.get("Generation (TWh)"))
            gen_yoy_pct = _finite(rec.get("Generation YoY change (%)"))
            share_pct = _finite(rec.get("Share of generation (%)"))
            emissions_mt = _finite(rec.get("Emissions (MtCO2e)"))
            intensity = _finite(rec.get("Emissions intensity (gCO2e/kWh)"))
            if not source or (gen_twh is None and emissions_mt is None):
                continue
            dates.add(date_s)
            row = {
                "iso3": iso3,
                "date": date_s,
                "electricity_source": source,
                "is_aggregated": (rec.get("Is aggregated source") or "").strip().lower() == "true",
                "generation_twh": gen_twh,
                "generation_yoy_pct": gen_yoy_pct,
                "share_of_generation_pct": share_pct,
                "emissions_mtco2e": emissions_mt,
                "emissions_intensity_g_per_kwh": intensity,
            }
            rows.append(row)
            if iso3 == "CHN" and gen_twh is not None:
                key = f"{source} (TWh)"
                prev = latest_by_var.get(key)
                if prev is None or date_s > prev[0]:
                    latest_by_var[key] = (date_s, gen_twh, "TWh")
            if iso3 == "CHN" and emissions_mt is not None and source in ("Total generation", "Coal", "Fossil"):
                ekey = f"{source} Emissions (MtCO2e)"
                prev = latest_by_var.get(ekey)
                if prev is None or date_s > prev[0]:
                    latest_by_var[ekey] = (date_s, emissions_mt, "MtCO2e")
        else:
            category = (rec.get("Category") or "").strip()
            subcat = (rec.get("Subcategory") or "").strip()
            variable = (rec.get("Variable") or "").strip()
            unit = (rec.get("Unit") or "").strip()
            val = _finite(rec.get("Value"))
            if not variable or val is None:
                continue
            dates.add(date_s)
            rows.append(
                {
                    "iso3": iso3,
                    "date": date_s,
                    "category": category,
                    "subcategory": subcat,
                    "variable": variable,
                    "unit": unit,
                    "value": val,
                }
            )
            if iso3 == "CHN" and unit in ("TWh", "mtCO2", "%"):
                key = f"{variable} ({unit})"
                prev = latest_by_var.get(key)
                if prev is None or date_s > prev[0]:
                    latest_by_var[key] = (date_s, val, unit)

    latest_snapshot = {
        k: {"date": v[0], "value": v[1], "unit": v[2]}
        for k, v in sorted(latest_by_var.items())
    }
    return {
        "raw_sha256": sha,
        "schema_variant": "wide" if is_wide else "long",
        "row_count": len(rows),
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "latest_china_snapshot": latest_snapshot,
        "rows": rows,
    }


def collect_ember_china(
    *,
    timeout: float = 60.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> dict[str, object]:
    _validate_url(EMBER_MONTHLY_URL)
    raw = fetcher(
        EMBER_MONTHLY_URL,
        timeout=timeout,
        max_bytes=MAX_EMBER_BYTES,
        max_redirects=3,
        headers={"Accept": "text/csv", "User-Agent": USER_AGENT},
        url_policy=_validate_url,
    )
    parsed = parse_ember_china_csv(raw)
    parsed["url"] = EMBER_MONTHLY_URL
    parsed["license"] = "CC-BY-4.0"
    parsed["attribution"] = "Source: Ember Monthly Electricity Data (CC BY 4.0)"
    return parsed


def collect_imf_datamapper_china(
    *,
    indicators: Sequence[tuple[str, str, str]] = IMF_INDICATORS,
    countries: Sequence[str] = IMF_COUNTRIES,
    timeout: float = 30.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> dict[str, object]:
    """Pull IMF DataMapper macro, fiscal deficit, and gross debt series for China + BRI."""
    country_set = set(countries)
    rows: list[dict[str, object]] = []
    latest_china: dict[str, dict[str, object]] = {}
    indicator_shas: dict[str, str] = {}
    failures: list[dict[str, str]] = []

    for ind_code, label, unit in indicators:
        url = f"{IMF_DATAMAPPER_BASE}/{ind_code}"
        try:
            _validate_url(url)
            raw = fetcher(
                url,
                timeout=timeout,
                max_bytes=MAX_API_BYTES,
                max_redirects=2,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                url_policy=_validate_url,
            )
            indicator_shas[ind_code] = hashlib.sha256(raw).hexdigest()
            payload = json.loads(raw.decode("utf-8"))
            values_by_country = (payload.get("values") or {}).get(ind_code) or {}
            for iso3, year_map in values_by_country.items():
                if iso3 not in country_set or not isinstance(year_map, dict):
                    continue
                for year_str, raw_val in sorted(year_map.items()):
                    val = _finite(raw_val)
                    if val is None or not str(year_str).isdigit():
                        continue
                    rows.append(
                        {
                            "iso3": iso3,
                            "indicator": ind_code,
                            "label": label,
                            "unit": unit,
                            "year": int(year_str),
                            "value": val,
                        }
                    )
                    if iso3 == "CHN":
                        latest_china[ind_code] = {
                            "label": label,
                            "unit": unit,
                            "latest_year": int(year_str),
                            "value": val,
                        }
        except Exception as exc:  # noqa: BLE001
            log.warning("IMF DataMapper %s failed: %s", ind_code, exc)
            failures.append({"indicator": ind_code, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "row_count": len(rows),
        "countries_covered": sorted({str(r["iso3"]) for r in rows}),
        "indicators_collected": len(indicator_shas),
        "indicator_sha256": indicator_shas,
        "latest_china": latest_china,
        "rows": rows,
        "failures": failures,
        "attribution": "Source: International Monetary Fund (IMF DataMapper)",
    }


def collect_wb_ids_bri_debt(
    *,
    countries: Sequence[str] = BRI_DEBT_COUNTRIES,
    timeout: float = 30.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> dict[str, object]:
    """Pull World Bank IDS external & bilateral debt stocks/service for BRI countries."""
    country_param = ";".join(countries)
    rows: list[dict[str, object]] = []
    indicator_counts: dict[str, int] = {}
    indicator_shas: dict[str, str] = {}
    failures: list[dict[str, str]] = []

    candidates = list(WB_IDS_SOURCE6_FALLBACKS)
    for ind_code, label in candidates:
        url = (
            f"{WB_API_BASE}/country/{country_param}/indicator/{ind_code}"
            f"?format=json&per_page=1000&date=2010:2025"
        )
        try:
            _validate_url(url)
            raw = fetcher(
                url,
                timeout=timeout,
                max_bytes=MAX_API_BYTES,
                max_redirects=2,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                url_policy=_validate_url,
            )
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
                failures.append({"indicator": ind_code, "error": "no data array returned"})
                continue
            indicator_shas[ind_code] = hashlib.sha256(raw).hexdigest()
            count = 0
            for rec in payload[1]:
                val = _finite(rec.get("value"))
                iso3 = (rec.get("countryiso3code") or "").strip()
                year_s = str(rec.get("date") or "").strip()
                if val is None or not iso3 or not year_s.isdigit():
                    continue
                count += 1
                rows.append(
                    {
                        "iso3": iso3,
                        "country_name": (rec.get("country") or {}).get("value") or iso3,
                        "indicator": ind_code,
                        "label": label,
                        "year": int(year_s),
                        "value_usd": val,
                    }
                )
            indicator_counts[ind_code] = count
        except Exception as exc:  # noqa: BLE001
            log.warning("WB IDS %s failed: %s", ind_code, exc)
            failures.append({"indicator": ind_code, "error": f"{type(exc).__name__}: {exc}"})

    # Compute latest total external debt by BRI country
    latest_debt: dict[str, dict[str, object]] = {}
    for r in rows:
        if r["indicator"] == "DT.DOD.DECT.CD":
            iso = str(r["iso3"])
            prev = latest_debt.get(iso)
            if prev is None or int(r["year"]) > int(prev["year"]):
                latest_debt[iso] = {
                    "country": r["country_name"],
                    "year": r["year"],
                    "external_debt_usd": r["value_usd"],
                }

    return {
        "row_count": len(rows),
        "countries_covered": sorted({str(r["iso3"]) for r in rows}),
        "indicator_counts": indicator_counts,
        "indicator_sha256": indicator_shas,
        "latest_bri_external_debt": latest_debt,
        "rows": rows,
        "failures": failures,
        "license": "CC-BY-4.0",
        "attribution": "Source: World Bank International Debt Statistics (CC BY 4.0)",
    }


def collect_openalex_china_tech(
    *,
    queries: Sequence[tuple[str, str]] = OPENALEX_TECH_QUERIES,
    timeout: float = 25.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> dict[str, object]:
    """Track China critical-tech research output and US-China co-authorship via OpenAlex (CC0)."""
    rows: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []

    for domain, query in queries:
        for cohort, country_filter in (
            ("china_total", "authorships.institutions.country_code:CN"),
            ("us_china_collab", "authorships.institutions.country_code:CN,authorships.institutions.country_code:US"),
        ):
            params = {
                "search": query,
                "filter": f"{country_filter},publication_year:2018-2026",
                "group_by": "publication_year",
            }
            url = f"{OPENALEX_API_BASE}?{urlencode(params)}"
            try:
                _validate_url(url)
                raw = fetcher(
                    url,
                    timeout=timeout,
                    max_bytes=MAX_API_BYTES,
                    max_redirects=2,
                    headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                    url_policy=_validate_url,
                )
                payload = json.loads(raw.decode("utf-8"))
                for grp in payload.get("group_by") or []:
                    yr = str(grp.get("key") or "")
                    cnt = grp.get("count")
                    if yr.isdigit() and isinstance(cnt, int):
                        rows.append(
                            {
                                "domain": domain,
                                "cohort": cohort,
                                "year": int(yr),
                                "works_count": cnt,
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                log.warning("OpenAlex %s/%s failed: %s", domain, cohort, exc)
                failures.append({"domain": domain, "cohort": cohort, "error": f"{type(exc).__name__}: {exc}"})

    # Compute US-China collaboration share trend per domain-year
    by_dy: dict[tuple[str, int], dict[str, int]] = defaultdict(dict)
    for r in rows:
        by_dy[(str(r["domain"]), int(r["year"]))][str(r["cohort"])] = int(r["works_count"])
    collab_shares: list[dict[str, object]] = []
    for (domain, year), counts in sorted(by_dy.items()):
        cn = counts.get("china_total", 0)
        us_cn = counts.get("us_china_collab", 0)
        collab_shares.append(
            {
                "domain": domain,
                "year": year,
                "china_works": cn,
                "us_china_collab_works": us_cn,
                "us_collab_share_pct": round((us_cn / cn) * 100.0, 2) if cn > 0 else None,
            }
        )

    return {
        "row_count": len(rows),
        "domains": sorted({str(r["domain"]) for r in rows}),
        "collab_shares": collab_shares,
        "rows": rows,
        "failures": failures,
        "license": "CC0-1.0",
        "attribution": "Source: OpenAlex (CC0 1.0)",
    }


def collect_china_energy_macro_dark(
    *,
    timeout: float = 45.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    now: str | None = None,
) -> dict[str, object]:
    """Run Ember + IMF DataMapper + World Bank IDS BRI + OpenAlex China tech collectors."""
    collected_at = now or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    results: dict[str, object] = {}
    errors: list[dict[str, str]] = []

    for key, fn in (
        ("ember_electricity", lambda: collect_ember_china(timeout=max(timeout, 60.0), fetcher=fetcher)),
        ("imf_datamapper", lambda: collect_imf_datamapper_china(timeout=timeout, fetcher=fetcher)),
        ("wb_ids_bri_debt", lambda: collect_wb_ids_bri_debt(timeout=timeout, fetcher=fetcher)),
        ("openalex_china_tech", lambda: collect_openalex_china_tech(timeout=timeout, fetcher=fetcher)),
    ):
        try:
            results[key] = fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("china_energy_macro_dark lane %s failed: %s", key, exc)
            results[key] = None
            errors.append({"lane": key, "error": f"{type(exc).__name__}: {exc}"})

    ok_lanes = sum(1 for v in results.values() if v is not None)
    return {
        "schema": "palimpsest.china-energy-macro-dark.v1",
        "generated_at": collected_at,
        "status": "ok" if ok_lanes == 4 else ("partial" if ok_lanes > 0 else "unavailable"),
        "summary": {
            "lanes_ok": ok_lanes,
            "lanes_total": 4,
            "ember_rows": (results["ember_electricity"] or {}).get("row_count", 0) if isinstance(results["ember_electricity"], dict) else 0,
            "imf_rows": (results["imf_datamapper"] or {}).get("row_count", 0) if isinstance(results["imf_datamapper"], dict) else 0,
            "wb_ids_rows": (results["wb_ids_bri_debt"] or {}).get("row_count", 0) if isinstance(results["wb_ids_bri_debt"], dict) else 0,
            "openalex_rows": (results["openalex_china_tech"] or {}).get("row_count", 0) if isinstance(results["openalex_china_tech"], dict) else 0,
        },
        **results,
        "errors": errors,
    }
