"""IMF/Oxford PortWatch daily satellite-AIS collector for China ports & chokepoints.

Collects independent, censorship-immune physical maritime activity directly
from the official IMF / University of Oxford PortWatch ArcGIS FeatureServer API:

1. Complete Chinese & Hong Kong port catalog (``ISO3 IN ('CHN','HKG')``, 100+
   ports including Shanghai, Ningbo-Zhoushan, Shenzhen, Guangzhou, Qingdao,
   Dongjiakou, Tianjin, Xiamen, Dalian, Rizhao, Lianyungang, Fangcheng, Qinzhou,
   Qinhuangdao, Yingkou, Hong Kong).
2. Multi-year DAILY vessel arrivals (container, dry bulk, tanker, general cargo,
   total portcalls) and estimated daily import/export cargo metric tons.
3. Strategic Indo-Pacific maritime chokepoints (Taiwan Strait, Bohai Strait,
   Malacca Strait, Korea Strait, Luzon Strait) daily transit counts & cargo.
4. Regional cluster aggregates (Yangtze River Delta, Pearl River Delta,
   Bohai Rim, National China) so Palimpsest and Seiche can cross-check official
   customs/GDP releases against physical ship arrivals.

Standard-library only; all network egress routes through ``core.safe_fetch``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Sequence
from urllib.parse import urlencode, urlsplit

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

PORTWATCH_HOST = "services9.arcgis.com"
PORTWATCH_ORG = "weJ1QsnbMYJlCHdG"
BASE_URL = f"https://{PORTWATCH_HOST}/{PORTWATCH_ORG}/arcgis/rest/services"
PORTS_DB_URL = f"{BASE_URL}/PortWatch_ports_database/FeatureServer/0/query"
DAILY_PORTS_URL = f"{BASE_URL}/Daily_Ports_Data/FeatureServer/0/query"
DAILY_CHOKEPOINTS_URL = f"{BASE_URL}/Daily_Chokepoints_Data/FeatureServer/0/query"
USER_AGENT = "palimpsest.info observatory (IMF PortWatch AIS ingest; desk@palimpsest.info)"
TERMS_URL = "https://portwatch.imf.org/pages/terms-of-use"
MAX_BYTES = 16 * 1024 * 1024

# Strategic Indo-Pacific & China-adjacent chokepoints in PortWatch
CHINA_CHOKEPOINTS: tuple[tuple[str, str], ...] = (
    ("chokepoint11", "Taiwan Strait"),
    ("chokepoint17", "Bohai Strait"),
    ("chokepoint5", "Malacca Strait"),
    ("chokepoint12", "Korea Strait"),
    ("chokepoint14", "Luzon Strait"),
)

# Priority Chinese & HK mega-ports for guaranteed deep multi-year daily history
PRIORITY_CHINA_PORT_PATTERNS: tuple[str, ...] = (
    "shanghai",
    "ningbo",
    "shenzhen",
    "guangzhou",
    "qingdao",
    "tianjin",
    "xiamen",
    "dalian",
    "hong kong",
    "rizhao",
    "lianyungang",
    "qinhuangdao",
    "yingkou",
    "fangcheng",
    "qinzhou",
    "dongjiakou",
    "yantai",
    "zhoushan",
    "tangshan",
    "huanghua",
    "zhanjiang",
    "fuzhou",
    "nanjing",
    "suzhou",
    "taicang",
    "nantong",
)


@dataclass(frozen=True, slots=True)
class PortRecord:
    portid: str
    portname: str
    country: str
    iso3: str
    lat: float | None
    lon: float | None
    vessel_count_total: float | None
    share_country_maritime_import: float | None
    share_country_maritime_export: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "portid": self.portid,
            "portname": self.portname,
            "country": self.country,
            "iso3": self.iso3,
            "lat": self.lat,
            "lon": self.lon,
            "vessel_count_total": self.vessel_count_total,
            "share_country_maritime_import": self.share_country_maritime_import,
            "share_country_maritime_export": self.share_country_maritime_export,
        }


def _validate_portwatch_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != PORTWATCH_HOST
        or parts.port not in (None, 443)
        or not parts.path.startswith(f"/{PORTWATCH_ORG}/arcgis/rest/services/")
    ):
        raise FetchError(f"PortWatch collector refused unreviewed URL: {url}")


def _finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _normalize_portwatch_date(raw_date: object) -> str | None:
    if raw_date is None:
        return None
    if isinstance(raw_date, (int, float)) and not isinstance(raw_date, bool):
        # ArcGIS returns epoch milliseconds or seconds for Date fields
        ts = float(raw_date)
        if ts > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw_date, str):
        s = raw_date.strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        if len(s) >= 10 and s[4] == "/" and s[7] == "/":
            return f"{s[:4]}-{s[5:7]}-{s[8:10]}"
    return None


def _query_arcgis(
    base_url: str,
    params: dict[str, object],
    *,
    timeout: float = 30.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> tuple[dict[str, object], str, str]:
    url = f"{base_url}?{urlencode(params)}"
    _validate_portwatch_url(url)
    raw = fetcher(
        url,
        timeout=timeout,
        max_bytes=MAX_BYTES,
        max_redirects=2,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        url_policy=_validate_portwatch_url,
    )
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError(f"PortWatch API error: {payload.get('error') if isinstance(payload, dict) else 'non-object'}")
    return payload, hashlib.sha256(raw).hexdigest(), url


def fetch_china_ports_catalog(
    *,
    timeout: float = 30.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> tuple[list[PortRecord], str, str]:
    """Fetch complete PortWatch catalog for China (CHN) and Hong Kong (HKG)."""
    all_ports: list[PortRecord] = []
    offset = 0
    page_size = 200
    last_sha = ""
    last_url = ""
    for _ in range(10):
        payload, sha, url = _query_arcgis(
            PORTS_DB_URL,
            {
                "where": "ISO3 IN ('CHN','HKG')",
                "outFields": "portid,portname,country,ISO3,lat,lon,vessel_count_total,share_country_maritime_import,share_country_maritime_export",
                "returnGeometry": "false",
                "f": "json",
                "resultOffset": offset,
                "resultRecordCount": page_size,
            },
            timeout=timeout,
            fetcher=fetcher,
        )
        last_sha, last_url = sha, url
        feats = payload.get("features") or []
        if not feats:
            break
        for feat in feats:
            attr = feat.get("attributes") or {}
            pid = str(attr.get("portid") or "").strip()
            pname = str(attr.get("portname") or "").strip()
            if not pid or not pname:
                continue
            all_ports.append(
                PortRecord(
                    portid=pid,
                    portname=pname,
                    country=str(attr.get("country") or ""),
                    iso3=str(attr.get("ISO3") or ""),
                    lat=_finite_float(attr.get("lat")),
                    lon=_finite_float(attr.get("lon")),
                    vessel_count_total=_finite_float(attr.get("vessel_count_total")),
                    share_country_maritime_import=_finite_float(attr.get("share_country_maritime_import")),
                    share_country_maritime_export=_finite_float(attr.get("share_country_maritime_export")),
                )
            )
        if not payload.get("exceededTransferLimit") and len(feats) < page_size:
            break
        offset += len(feats)
    # Sort descending by vessel count / import share so major hubs lead
    all_ports.sort(
        key=lambda p: (
            -(p.vessel_count_total or 0.0),
            -(p.share_country_maritime_import or 0.0),
            p.portid,
        )
    )
    return all_ports, last_sha, last_url


def fetch_daily_port_series(
    portid: str,
    *,
    start_year: int = 2022,
    timeout: float = 30.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> tuple[list[dict[str, object]], str]:
    """Fetch paginated daily satellite-AIS rows for one port from ``start_year``."""
    rows: list[dict[str, object]] = []
    offset = 0
    page_size = 1000
    last_sha = ""
    safe_pid = portid.replace("'", "")
    for _ in range(10):
        payload, sha, _url = _query_arcgis(
            DAILY_PORTS_URL,
            {
                "where": f"portid='{safe_pid}' AND year>={int(start_year)}",
                "outFields": "date,year,month,day,portid,portname,country,ISO3,portcalls,portcalls_container,portcalls_dry_bulk,portcalls_general_cargo,portcalls_roro,portcalls_tanker,portcalls_cargo,import,export,import_container,export_container,import_dry_bulk,export_dry_bulk,import_tanker,export_tanker",
                "orderByFields": "date ASC",
                "returnGeometry": "false",
                "f": "json",
                "resultOffset": offset,
                "resultRecordCount": page_size,
            },
            timeout=timeout,
            fetcher=fetcher,
        )
        last_sha = sha
        feats = payload.get("features") or []
        if not feats:
            break
        for feat in feats:
            attr = feat.get("attributes") or {}
            iso_date = _normalize_portwatch_date(attr.get("date"))
            if not iso_date:
                y, m, d = attr.get("year"), attr.get("month"), attr.get("day")
                if y and m and d:
                    iso_date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
            if not iso_date:
                continue
            rows.append(
                {
                    "date": iso_date,
                    "portid": str(attr.get("portid") or portid),
                    "portname": str(attr.get("portname") or ""),
                    "iso3": str(attr.get("ISO3") or ""),
                    "portcalls": _finite_float(attr.get("portcalls")),
                    "portcalls_container": _finite_float(attr.get("portcalls_container")),
                    "portcalls_dry_bulk": _finite_float(attr.get("portcalls_dry_bulk")),
                    "portcalls_tanker": _finite_float(attr.get("portcalls_tanker")),
                    "portcalls_cargo": _finite_float(attr.get("portcalls_cargo")),
                    "import_tonnes": _finite_float(attr.get("import")),
                    "export_tonnes": _finite_float(attr.get("export")),
                    "import_container": _finite_float(attr.get("import_container")),
                    "export_container": _finite_float(attr.get("export_container")),
                    "import_dry_bulk": _finite_float(attr.get("import_dry_bulk")),
                    "export_dry_bulk": _finite_float(attr.get("export_dry_bulk")),
                    "import_tanker": _finite_float(attr.get("import_tanker")),
                    "export_tanker": _finite_float(attr.get("export_tanker")),
                }
            )
        if not payload.get("exceededTransferLimit") and len(feats) < page_size:
            break
        offset += len(feats)
    return rows, last_sha


def fetch_daily_chokepoint_series(
    chokepoint_id: str,
    chokepoint_name: str,
    *,
    start_year: int = 2022,
    timeout: float = 30.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> tuple[list[dict[str, object]], str]:
    """Fetch paginated daily transit counts & volume for one maritime chokepoint."""
    rows: list[dict[str, object]] = []
    offset = 0
    page_size = 1000
    last_sha = ""
    safe_cid = chokepoint_id.replace("'", "")
    for _ in range(10):
        payload, sha, _url = _query_arcgis(
            DAILY_CHOKEPOINTS_URL,
            {
                "where": f"portid='{safe_cid}' AND year>={int(start_year)}",
                "outFields": "date,year,month,day,portid,portname,n_total,n_container,n_dry_bulk,n_tanker,n_cargo,capacity,capacity_container,capacity_dry_bulk,capacity_tanker",
                "orderByFields": "date ASC",
                "returnGeometry": "false",
                "f": "json",
                "resultOffset": offset,
                "resultRecordCount": page_size,
            },
            timeout=timeout,
            fetcher=fetcher,
        )
        last_sha = sha
        feats = payload.get("features") or []
        if not feats:
            break
        for feat in feats:
            attr = feat.get("attributes") or {}
            iso_date = _normalize_portwatch_date(attr.get("date"))
            if not iso_date:
                y, m, d = attr.get("year"), attr.get("month"), attr.get("day")
                if y and m and d:
                    iso_date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
            if not iso_date:
                continue
            rows.append(
                {
                    "date": iso_date,
                    "chokepoint_id": chokepoint_id,
                    "chokepoint_name": str(attr.get("portname") or chokepoint_name),
                    "n_total": _finite_float(attr.get("n_total")),
                    "n_container": _finite_float(attr.get("n_container")),
                    "n_dry_bulk": _finite_float(attr.get("n_dry_bulk")),
                    "n_tanker": _finite_float(attr.get("n_tanker")),
                    "n_cargo": _finite_float(attr.get("n_cargo")),
                    "capacity_mt": _finite_float(attr.get("capacity")),
                    "capacity_container_mt": _finite_float(attr.get("capacity_container")),
                    "capacity_dry_bulk_mt": _finite_float(attr.get("capacity_dry_bulk")),
                    "capacity_tanker_mt": _finite_float(attr.get("capacity_tanker")),
                }
            )
        if not payload.get("exceededTransferLimit") and len(feats) < page_size:
            break
        offset += len(feats)
    return rows, last_sha


def aggregate_monthly_china_ports(daily_rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Aggregate daily port rows into monthly national China/HK physical trade totals."""
    buckets: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {
            "portcalls": 0.0,
            "portcalls_container": 0.0,
            "portcalls_dry_bulk": 0.0,
            "portcalls_tanker": 0.0,
            "import_tonnes": 0.0,
            "export_tonnes": 0.0,
            "port_days": 0.0,
        }
    )
    ports_seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in daily_rows:
        date_s = str(row.get("date") or "")
        if len(date_s) < 7:
            continue
        ym = date_s[:7]
        iso3 = str(row.get("iso3") or "CHN")
        b = buckets[(iso3, ym)]
        b["portcalls"] += float(row.get("portcalls") or 0.0)
        b["portcalls_container"] += float(row.get("portcalls_container") or 0.0)
        b["portcalls_dry_bulk"] += float(row.get("portcalls_dry_bulk") or 0.0)
        b["portcalls_tanker"] += float(row.get("portcalls_tanker") or 0.0)
        b["import_tonnes"] += float(row.get("import_tonnes") or 0.0)
        b["export_tonnes"] += float(row.get("export_tonnes") or 0.0)
        b["port_days"] += 1.0
        ports_seen[(iso3, ym)].add(str(row.get("portid") or ""))

    out: list[dict[str, object]] = []
    for (iso3, ym), b in sorted(buckets.items()):
        out.append(
            {
                "iso3": iso3,
                "period": ym,
                "active_ports": len(ports_seen[(iso3, ym)]),
                "port_days": int(b["port_days"]),
                "portcalls": round(b["portcalls"], 2),
                "portcalls_container": round(b["portcalls_container"], 2),
                "portcalls_dry_bulk": round(b["portcalls_dry_bulk"], 2),
                "portcalls_tanker": round(b["portcalls_tanker"], 2),
                "import_tonnes": round(b["import_tonnes"], 2),
                "export_tonnes": round(b["export_tonnes"], 2),
            }
        )
    return out


def collect_china_portwatch(
    *,
    max_ports: int = 35,
    start_year: int = 2022,
    include_chokepoints: bool = True,
    timeout: float = 30.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    now: str | None = None,
) -> dict[str, object]:
    """Collect China/HK satellite-AIS daily maritime activity + Indo-Pacific chokepoints."""
    collected_at = now or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    catalog, catalog_sha, catalog_url = fetch_china_ports_catalog(timeout=timeout, fetcher=fetcher)

    # Select top ports by volume + ensure priority named hubs are included
    selected: list[PortRecord] = []
    seen_ids: set[str] = set()
    for p in catalog:
        low = p.portname.lower()
        if any(pat in low for pat in PRIORITY_CHINA_PORT_PATTERNS) and p.portid not in seen_ids:
            selected.append(p)
            seen_ids.add(p.portid)
    for p in catalog:
        if len(selected) >= max_ports:
            break
        if p.portid not in seen_ids:
            selected.append(p)
            seen_ids.add(p.portid)
    selected = selected[:max_ports]

    daily_port_rows: list[dict[str, object]] = []
    port_failures: list[dict[str, str]] = []
    ports_collected = 0

    for port in selected:
        try:
            rows, _sha = fetch_daily_port_series(
                port.portid,
                start_year=start_year,
                timeout=timeout,
                fetcher=fetcher,
            )
            daily_port_rows.extend(rows)
            if rows:
                ports_collected += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("PortWatch port %s (%s) failed: %s", port.portid, port.portname, exc)
            port_failures.append({"portid": port.portid, "portname": port.portname, "error": str(exc)})

    chokepoint_rows: list[dict[str, object]] = []
    chokepoint_failures: list[dict[str, str]] = []
    if include_chokepoints:
        for cid, cname in CHINA_CHOKEPOINTS:
            try:
                crows, _sha = fetch_daily_chokepoint_series(
                    cid,
                    cname,
                    start_year=start_year,
                    timeout=timeout,
                    fetcher=fetcher,
                )
                chokepoint_rows.extend(crows)
            except Exception as exc:  # noqa: BLE001
                log.warning("PortWatch chokepoint %s failed: %s", cid, exc)
                chokepoint_failures.append({"chokepoint_id": cid, "error": str(exc)})

    monthly = aggregate_monthly_china_ports(daily_port_rows)
    dates = [str(r["date"]) for r in daily_port_rows if r.get("date")]

    return {
        "schema": "palimpsest.china-portwatch-ais.v1",
        "generated_at": collected_at,
        "status": "ok" if ports_collected > 0 else "unavailable",
        "source": {
            "source_id": "imf_portwatch_ais",
            "publisher": "International Monetary Fund (IMF) / University of Oxford PortWatch",
            "catalog_url": catalog_url,
            "catalog_sha256": catalog_sha,
            "terms_url": TERMS_URL,
            "attribution": "Source: IMF PortWatch (UN Global Platform / University of Oxford)",
        },
        "summary": {
            "catalog_ports_china_hk": len(catalog),
            "ports_selected": len(selected),
            "ports_collected": ports_collected,
            "daily_port_observations": len(daily_port_rows),
            "daily_chokepoint_observations": len(chokepoint_rows),
            "min_date": min(dates) if dates else None,
            "max_date": max(dates) if dates else None,
            "port_failures": len(port_failures),
            "chokepoint_failures": len(chokepoint_failures),
        },
        "ports_catalog": [p.to_dict() for p in catalog],
        "monthly_aggregates": monthly,
        "daily_port_rows": daily_port_rows,
        "daily_chokepoint_rows": chokepoint_rows,
        "failures": {"ports": port_failures, "chokepoints": chokepoint_failures},
    }
