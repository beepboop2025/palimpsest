"""GDELT 2.0 China sub-national dissent/protest/stress events & Common Crawl multi-crawl watch.

Closes two major China "dark data" blind spots:

1. **GDELT 2.0 Live China Sub-National Event Stream** (commercial & redistribution
   permitted with citation per GDELT About terms):
   Fetches the live GDELT 2.0 15-minute export feed (plus optional recent multi-
   interval backfill), filters all events involving China / Hong Kong / Taiwan /
   Macau (``Actor1CountryCode`` / ``Actor2CountryCode`` / ``ActionGeo_CountryCode``
   in ``{'CHN', 'CH', 'HKG', 'HK', 'TWN', 'TW', 'MAC', 'MC'}``), and classifies:
   - Domestic protest, demonstration, strike, boycott (CAMEO root 14)
   - Coercion, repression, arrest, censorship, crackdown (CAMEO root 17)
   - Armed/security clash or military posture (CAMEO roots 13, 18, 19)
   - Economic / trade / sanctions / diplomatic tension (CAMEO roots 11, 12, 16)
   - Provincial/city geocoded coordinates, Goldstein scale, tone, mention counts.
2. **Common Crawl Multi-Crawl Index Readiness Monitor** (index metadata only;
   ZERO WARC downloads):
   Reads ``https://index.commoncrawl.org/collinfo.json`` and compares all
   available 2025–2026 crawls against Palimpsest's currently ingested crawls
   (e.g., ``CC-MAIN-2026-30``), emitting an exact actionable queue of un-ingested
   index-only JSONL crawls (such as ``CC-MAIN-2026-34``, ``39``, ``43``) so
   Palimpsest reaches the >=6 crawl threshold needed to unlock ``mutation_rate``,
   ``archive_gap``, and ``error_rate MAD`` across all 10 Chinese official hosts.

Standard-library only; all egress routes through ``core.safe_fetch``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Callable, Sequence
from urllib.parse import urlsplit

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

GDELT_LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_BASE_PREFIX = "https://data.gdeltproject.org/gdeltv2/"
COMMON_CRAWL_COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
USER_AGENT = "palimpsest.info observatory (GDELT & CC index watch; desk@palimpsest.info)"

_ALLOWED_HOSTS = frozenset({
    "data.gdeltproject.org",
    "index.commoncrawl.org",
})
MAX_ZIP_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 96 * 1024 * 1024

# GDELT 2.0 Event table column indices (61 tab-separated columns, no header row)
_COL_GLOBALEVENTID = 0
_COL_SQLDATE = 1
_COL_ACTOR1_CODE = 5
_COL_ACTOR1_NAME = 6
_COL_ACTOR1_COUNTRY = 7
_COL_ACTOR2_CODE = 15
_COL_ACTOR2_NAME = 16
_COL_ACTOR2_COUNTRY = 17
_COL_EVENT_CODE = 26
_COL_EVENT_BASE = 27
_COL_EVENT_ROOT = 28
_COL_QUAD_CLASS = 29
_COL_GOLDSTEIN = 30
_COL_NUM_MENTIONS = 31
_COL_NUM_SOURCES = 32
_COL_AVG_TONE = 34
_COL_ACTION_GEO_NAME = 52
_COL_ACTION_GEO_COUNTRY = 53
_COL_ACTION_GEO_ADM1 = 54
_COL_ACTION_GEO_LAT = 56
_COL_ACTION_GEO_LON = 57
_COL_DATEADDED = 59
_COL_SOURCEURL = 60

_CHINA_ISO = frozenset({"CHN", "HKG", "TWN", "MAC"})
_CHINA_FIPS = frozenset({"CH", "HK", "TW", "MC"})

_CAMEO_CATEGORY = {
    "14": "protest_dissent_strike",
    "17": "coercion_repression_censorship",
    "16": "reduce_relations_sanctions",
    "13": "threaten_military_posture",
    "18": "assault_unrest",
    "19": "armed_clash",
    "11": "disapprove_criticism",
    "12": "reject_dispute",
}


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or parts.hostname not in _ALLOWED_HOSTS:
        raise FetchError(f"china_gdelt_subnational refused unreviewed URL: {url}")


def _to_https_gdelt(url: str) -> str:
    if url.startswith("http://data.gdeltproject.org/"):
        return "https://" + url[len("http://"):]
    return url


def _safe_float(val: str) -> float | None:
    try:
        n = float(val)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _safe_int(val: str) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def parse_gdelt_export_zip(raw_zip: bytes, *, source_url: str) -> list[dict[str, object]]:
    """Parse one GDELT 2.0 export CSV zip and return all China/HK/TW/MO events."""
    events: list[dict[str, object]] = []
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        infos = zf.infolist()
        if not infos or len(infos) > 4:
            raise ValueError("unexpected GDELT zip member count")
        if sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
            raise ValueError("GDELT zip expansion exceeded safety cap")
        csv_bytes = zf.read(infos[0])

    reader = csv.reader(io.StringIO(csv_bytes.decode("utf-8", errors="replace")), delimiter="\t")
    for cols in reader:
        if len(cols) < 61:
            continue
        a1_cty = cols[_COL_ACTOR1_COUNTRY].strip().upper()
        a2_cty = cols[_COL_ACTOR2_COUNTRY].strip().upper()
        geo_cty = cols[_COL_ACTION_GEO_COUNTRY].strip().upper()
        if not (a1_cty in _CHINA_ISO or a2_cty in _CHINA_ISO or geo_cty in _CHINA_FIPS):
            continue

        root_code = cols[_COL_EVENT_ROOT].strip()
        event_code = cols[_COL_EVENT_CODE].strip()
        category = _CAMEO_CATEGORY.get(root_code, "cooperation_or_general")
        events.append(
            {
                "global_event_id": cols[_COL_GLOBALEVENTID].strip(),
                "sqldate": cols[_COL_SQLDATE].strip(),
                "date_added": cols[_COL_DATEADDED].strip(),
                "actor1_code": cols[_COL_ACTOR1_CODE].strip(),
                "actor1_name": cols[_COL_ACTOR1_NAME].strip(),
                "actor1_country": a1_cty,
                "actor2_code": cols[_COL_ACTOR2_CODE].strip(),
                "actor2_name": cols[_COL_ACTOR2_NAME].strip(),
                "actor2_country": a2_cty,
                "event_code": event_code,
                "event_base_code": cols[_COL_EVENT_BASE].strip(),
                "event_root_code": root_code,
                "category": category,
                "quad_class": _safe_int(cols[_COL_QUAD_CLASS].strip()),
                "goldstein_scale": _safe_float(cols[_COL_GOLDSTEIN].strip()),
                "num_mentions": _safe_int(cols[_COL_NUM_MENTIONS].strip()),
                "num_sources": _safe_int(cols[_COL_NUM_SOURCES].strip()),
                "avg_tone": _safe_float(cols[_COL_AVG_TONE].strip()),
                "action_geo_name": cols[_COL_ACTION_GEO_NAME].strip(),
                "action_geo_country": geo_cty,
                "action_geo_adm1": cols[_COL_ACTION_GEO_ADM1].strip(),
                "action_geo_lat": _safe_float(cols[_COL_ACTION_GEO_LAT].strip()),
                "action_geo_lon": _safe_float(cols[_COL_ACTION_GEO_LON].strip()),
                "source_url": cols[_COL_SOURCEURL].strip()[:500],
                "gdelt_batch_url": source_url,
            }
        )
    return events


def _discover_recent_gdelt_urls(
    lastupdate_text: str,
    *,
    num_intervals: int = 8,
) -> list[str]:
    """Build exact 15-minute GDELT export URLs going backward from lastupdate.txt."""
    latest_export_url = ""
    for line in lastupdate_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[2].endswith(".export.CSV.zip"):
            latest_export_url = _to_https_gdelt(parts[2])
            break
    if not latest_export_url:
        return []
    fname = latest_export_url.rsplit("/", 1)[-1]
    stamp = fname.split(".", 1)[0]
    if len(stamp) != 14 or not stamp.isdigit():
        return [latest_export_url]

    base_dt = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    urls: list[str] = []
    for step in range(max(1, num_intervals)):
        dt = base_dt - timedelta(minutes=15 * step)
        urls.append(f"{GDELT_BASE_PREFIX}{dt.strftime('%Y%m%d%H%M%S')}.export.CSV.zip")
    return urls


def collect_gdelt_china_subnational(
    *,
    num_intervals: int = 8,
    timeout: float = 25.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    now: str | None = None,
) -> dict[str, object]:
    """Collect recent GDELT 2.0 15-minute China sub-national events & dissent metrics."""
    collected_at = now or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _validate_url(GDELT_LASTUPDATE_URL)
    raw_lu = fetcher(
        GDELT_LASTUPDATE_URL,
        timeout=timeout,
        max_bytes=65536,
        max_redirects=2,
        headers={"User-Agent": USER_AGENT},
        url_policy=_validate_url,
    )
    urls = _discover_recent_gdelt_urls(raw_lu.decode("utf-8", errors="replace"), num_intervals=num_intervals)

    events: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    batch_shas: dict[str, str] = {}
    failures: list[dict[str, str]] = []

    for url in urls:
        try:
            _validate_url(url)
            raw_zip = fetcher(
                url,
                timeout=timeout,
                max_bytes=MAX_ZIP_BYTES,
                max_redirects=2,
                headers={"User-Agent": USER_AGENT},
                url_policy=_validate_url,
            )
            batch_shas[url] = hashlib.sha256(raw_zip).hexdigest()
            for ev in parse_gdelt_export_zip(raw_zip, source_url=url):
                eid = str(ev["global_event_id"])
                if eid not in seen_ids:
                    seen_ids.add(eid)
                    events.append(ev)
        except Exception as exc:  # noqa: BLE001
            log.warning("GDELT batch %s failed: %s", url, exc)
            failures.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})

    by_category = Counter(str(e["category"]) for e in events)
    by_adm1 = Counter(str(e["action_geo_adm1"]) for e in events if e.get("action_geo_adm1"))
    by_country_geo = Counter(str(e["action_geo_country"]) for e in events if e.get("action_geo_country"))
    tones = [float(e["avg_tone"]) for e in events if e.get("avg_tone") is not None]
    goldsteins = [float(e["goldstein_scale"]) for e in events if e.get("goldstein_scale") is not None]

    return {
        "schema": "palimpsest.china-gdelt-subnational.v1",
        "generated_at": collected_at,
        "status": "ok" if batch_shas else "unavailable",
        "source": {
            "source_id": "gdelt_china_subnational",
            "publisher": "The GDELT Project (GDELT 2.0 Global Event Database)",
            "terms_url": "https://www.gdeltproject.org/about.html",
            "attribution": "Source: The GDELT Project (https://www.gdeltproject.org/)",
        },
        "summary": {
            "intervals_requested": len(urls),
            "intervals_collected": len(batch_shas),
            "china_events_collected": len(events),
            "protest_dissent_events": by_category.get("protest_dissent_strike", 0),
            "coercion_censorship_events": by_category.get("coercion_repression_censorship", 0),
            "sanctions_dispute_events": by_category.get("reduce_relations_sanctions", 0),
            "mean_tone": round(sum(tones) / len(tones), 3) if tones else None,
            "mean_goldstein": round(sum(goldsteins) / len(goldsteins), 3) if goldsteins else None,
            "by_category": dict(by_category.most_common()),
            "by_geo_country": dict(by_country_geo.most_common()),
            "top_adm1_regions": dict(by_adm1.most_common(20)),
        },
        "batch_sha256": batch_shas,
        "events": events,
        "failures": failures,
    }


def collect_common_crawl_index_catalog(
    *,
    ingested_crawls: Sequence[str] = ("CC-MAIN-2026-30",),
    timeout: float = 25.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    now: str | None = None,
) -> dict[str, object]:
    """Audit Common Crawl collinfo.json and emit the exact index-only ingestion queue."""
    collected_at = now or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _validate_url(COMMON_CRAWL_COLLINFO_URL)
    raw = fetcher(
        COMMON_CRAWL_COLLINFO_URL,
        timeout=timeout,
        max_bytes=4 * 1024 * 1024,
        max_redirects=2,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        url_policy=_validate_url,
    )
    catalog = json.loads(raw.decode("utf-8"))
    if not isinstance(catalog, list):
        raise ValueError("Common Crawl collinfo.json was not a list")

    ingested = set(ingested_crawls)
    recent_2025_2026 = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "cdx_api": c.get("cdx-api"),
            "from": c.get("from"),
            "to": c.get("to"),
            "ingested": c.get("id") in ingested,
        }
        for c in catalog
        if isinstance(c, dict) and str(c.get("id") or "").startswith(("CC-MAIN-2026-", "CC-MAIN-2025-"))
    ]
    pending_2026 = [c for c in recent_2025_2026 if str(c["id"]).startswith("CC-MAIN-2026-") and not c["ingested"]]
    # At least 6 prior crawls are required to unlock error_rate MAD and mutation_rate scores
    recommended_next = [c["id"] for c in pending_2026[:6]]

    return {
        "schema": "palimpsest.common-crawl-index-watch.v1",
        "generated_at": collected_at,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "total_crawls_published": len(catalog),
        "latest_crawl": catalog[0].get("id") if catalog else None,
        "ingested_crawls": sorted(ingested),
        "pending_2026_crawls_count": len(pending_2026),
        "recommended_index_only_queue": recommended_next,
        "recent_crawls": recent_2025_2026[:20],
        "policy": "index_only_jsonl_no_warc_downloads",
    }
