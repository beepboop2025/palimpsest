"""Unified rights-preserving Fleet Data Bridge for Palimpsest.

Connects Palimpsest to ALL live data surfaces across the product fleet while
strictly preserving separate clocks, evidence classes, provenance URLs, and
upstream redistribution boundaries (never flattening products into one score or
relabeling structural/restricted metadata as observed values):

1. **Seiche** (``https://api.seiche.info/api/v2/money-markets`` &
   ``world-markets?section=capital_markets``): global funding regimes, live
   overnight benchmarks, and capital-market transmission context.
2. **LiquiLens** (``https://api.liquilens.in/api/failure-radar/board``):
   financial-institution risk diagnostics, active/stale coverage, and China
   ontology linkage context.
3. **Undertow** (``https://api.seiche.info/undertow/x402/summary``): market
   depth, exit-liquidity regime, and the ``CN`` liquidity segment status.
4. **Palimpsest-Nemesis** (local signed snapshot on ``fsn1`` at
   ``/var/lib/palimpsest-nemesis/public/osint-china.json`` when present):
   15-minute continuous China OSINT topic/novelty/economic-share signal.
5. **Palimpsest Bulk DuckDB Lake** (local read-only DuckDB at
   ``/var/lib/palimpsest/bulk-data/catalog/bulk.duckdb`` when present):
   12.08M+ analytical rows (World Bank WDI 9.01M, UNODC IDS 2.53M, OFR 544K,
   GDELT 2.0, UCDP 26.1, Eurostat Core) with China/HK/MO slice summaries.
6. **Financial-Collector SEC China ADR/VIE Bridge** (optional local/exported
   read-only JSON snapshot from ``hel1`` 97 GB SEC SQLite warehouse):
   US-listed Chinese foreign private issuers (20-F / 6-K) and financial facts.

Standard-library only (optional ``duckdb`` only if querying a local DuckDB file);
all HTTP egress routes through ``core.safe_fetch``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

SEICHE_MM_URL = "https://api.seiche.info/api/v2/money-markets"
SEICHE_CM_URL = "https://api.seiche.info/api/v2/world-markets?section=capital_markets"
LIQUILENS_BOARD_URL = "https://api.liquilens.in/api/failure-radar/board"
UNDERTOW_SUMMARY_URL = "https://api.seiche.info/undertow/x402/summary"
PALIMPSEST_HEALTH_URL = "https://palimpsest.info/readings/collector-health-latest.json"

DEFAULT_NEMESIS_SNAPSHOT = Path("/var/lib/palimpsest-nemesis/public/osint-china.json")
DEFAULT_BULK_METRICS = Path("/var/lib/palimpsest/bulk-data/public/evidence-lake-metrics-latest.json")
USER_AGENT = "palimpsest.info fleet-bridge/1.0 (desk@palimpsest.info)"

_ALLOWED_HOSTS = frozenset({
    "api.seiche.info",
    "api.liquilens.in",
    "palimpsest.info",
    "www.palimpsest.info",
})
MAX_BYTES = 16 * 1024 * 1024


def _validate_fleet_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS or parts.port not in (None, 443):
        raise FetchError(f"fleet_data_bridge refused unreviewed URL: {url}")


def _fetch_json(
    url: str,
    *,
    timeout: float = 20.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> tuple[dict[str, object], str, int]:
    _validate_fleet_url(url)
    raw = fetcher(
        url,
        timeout=timeout,
        max_bytes=MAX_BYTES,
        max_redirects=2,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        url_policy=_validate_fleet_url,
    )
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"fleet endpoint {url} did not return a JSON object")
    return payload, hashlib.sha256(raw).hexdigest(), len(raw)


def _summarize_seiche_money_markets(payload: dict[str, object], sha: str, byte_len: int, retrieved_at: str) -> dict[str, object]:
    markets = payload.get("markets") or []
    by_status: dict[str, int] = {}
    stale_or_unavailable: list[dict[str, object]] = []
    live_benchmarks: list[dict[str, object]] = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        st = str(m.get("status") or "UNKNOWN")
        by_status[st] = by_status.get(st, 0) + 1
        bench = m.get("benchmark") if isinstance(m.get("benchmark"), dict) else {}
        if st == "LIVE_REFERENCE":
            live_benchmarks.append(
                {
                    "market_id": m.get("market_id"),
                    "benchmark": bench.get("id"),
                    "value": bench.get("value"),
                    "unit": bench.get("unit"),
                    "asof": bench.get("asof"),
                    "source": bench.get("source"),
                }
            )
        else:
            stale_or_unavailable.append(
                {
                    "market_id": m.get("market_id"),
                    "status": st,
                    "benchmark": bench.get("id"),
                    "asof": bench.get("asof"),
                }
            )
    return {
        "product": "Seiche",
        "surface": "money_markets",
        "evidence_class": "observed_and_structural",
        "source_url": SEICHE_MM_URL,
        "retrieved_at": retrieved_at,
        "product_generated_at": payload.get("generated_at"),
        "status": payload.get("status"),
        "bytes": byte_len,
        "sha256": sha,
        "markets_total": len(markets),
        "by_status": by_status,
        "live_benchmarks": live_benchmarks,
        "non_live_markets": stale_or_unavailable,
    }


def _summarize_seiche_capital_markets(payload: dict[str, object], sha: str, byte_len: int, retrieved_at: str) -> dict[str, object]:
    return {
        "product": "Seiche",
        "surface": "capital_markets",
        "evidence_class": "derived",
        "source_url": SEICHE_CM_URL,
        "retrieved_at": retrieved_at,
        "product_generated_at": payload.get("generated_at"),
        "as_of": payload.get("as_of"),
        "status": payload.get("status"),
        "bytes": byte_len,
        "sha256": sha,
    }


def _summarize_liquilens(payload: dict[str, object], sha: str, byte_len: int, retrieved_at: str) -> dict[str, object]:
    tier = payload.get("tier_publication") if isinstance(payload.get("tier_publication"), dict) else {}
    return {
        "product": "LiquiLens",
        "surface": "failure_radar_board",
        "evidence_class": "derived",
        "source_url": LIQUILENS_BOARD_URL,
        "retrieved_at": retrieved_at,
        "as_of": payload.get("as_of"),
        "active_rows": len(payload.get("rows") or []),
        "excluded_stale_count": payload.get("excluded_stale_count"),
        "excluded_inactive_count": len(payload.get("excluded_inactive") or []),
        "tier_publication_state": tier.get("state"),
        "bytes": byte_len,
        "sha256": sha,
    }


def _summarize_undertow(payload: dict[str, object], sha: str, byte_len: int, retrieved_at: str) -> dict[str, object]:
    segs = payload.get("segments") if isinstance(payload.get("segments"), dict) else {}
    return {
        "product": "Undertow",
        "surface": "market_liquidity_summary",
        "evidence_class": "derived",
        "source_url": UNDERTOW_SUMMARY_URL,
        "retrieved_at": retrieved_at,
        "as_of": payload.get("asof"),
        "funding_regime": payload.get("funding_regime"),
        "segments": segs,
        "cn_segment_status": segs.get("CN"),
        "bytes": byte_len,
        "sha256": sha,
    }


def read_local_nemesis_snapshot(path: Path = DEFAULT_NEMESIS_SNAPSHOT) -> dict[str, object]:
    """Read local HMAC-signed Palimpsest-Nemesis public snapshot without network egress."""
    if not path.exists():
        return {"status": "not_present_on_this_host", "path": str(path)}
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    gen_ts = payload.get("generated_at")
    gen_iso = (
        datetime.fromtimestamp(float(gen_ts), tz=UTC).isoformat().replace("+00:00", "Z")
        if isinstance(gen_ts, (int, float))
        else str(gen_ts)
    )
    return {
        "product": "Palimpsest-Nemesis",
        "evidence_class": "observed_public_osint",
        "path": str(path),
        "status": payload.get("status"),
        "generated_at": gen_iso,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "counts": payload.get("counts"),
        "economic": payload.get("economic"),
        "leads_count": len(payload.get("leads") or []),
    }


def read_local_bulk_lake_metrics(path: Path = DEFAULT_BULK_METRICS) -> dict[str, object]:
    """Read local Palimpsest bulk DuckDB/Parquet lake metrics when running on fsn1."""
    if not path.exists():
        return {"status": "not_present_on_this_host", "path": str(path)}
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    return {
        "product": "Palimpsest-Bulk-Data-Plane",
        "evidence_class": "bulk_warehouse_metrics",
        "path": str(path),
        "generated_at": payload.get("generated_at"),
        "edition": payload.get("edition"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "summary": payload.get("summary"),
        "gates": payload.get("gates"),
        "lanes": [
            {
                "id": l.get("id"),
                "products": l.get("products"),
                "publication_state": l.get("publication_state"),
                "queryable_records": l.get("queryable_records"),
                "publication_eligible_records": l.get("publication_eligible_records"),
            }
            for l in (payload.get("lanes") or [])
            if isinstance(l, dict)
        ],
    }


def collect_fleet_bridge_snapshot(
    *,
    nemesis_path: Path = DEFAULT_NEMESIS_SNAPSHOT,
    bulk_metrics_path: Path = DEFAULT_BULK_METRICS,
    sec_china_extract_path: Path | None = None,
    timeout: float = 20.0,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
    now: str | None = None,
) -> dict[str, object]:
    """Build one unified, boundary-preserving cross-product fleet evidence snapshot."""
    retrieved_at = now or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    surfaces: dict[str, object] = {}
    errors: list[dict[str, str]] = []

    remote_specs = (
        ("seiche_money_markets", SEICHE_MM_URL, _summarize_seiche_money_markets),
        ("seiche_capital_markets", SEICHE_CM_URL, _summarize_seiche_capital_markets),
        ("liquilens_board", LIQUILENS_BOARD_URL, _summarize_liquilens),
        ("undertow_summary", UNDERTOW_SUMMARY_URL, _summarize_undertow),
    )
    for name, url, summarizer in remote_specs:
        try:
            payload, sha, byte_len = _fetch_json(url, timeout=timeout, fetcher=fetcher)
            surfaces[name] = summarizer(payload, sha, byte_len, retrieved_at)
        except Exception as exc:  # noqa: BLE001
            log.warning("Fleet bridge %s failed: %s", name, exc)
            surfaces[name] = {
                "status": "unavailable",
                "evidence_class": "unavailable",
                "source_url": url,
                "retrieved_at": retrieved_at,
                "error": f"{type(exc).__name__}: {exc}",
            }
            errors.append({"surface": name, "error": f"{type(exc).__name__}: {exc}"})

    # Local host surfaces (present on fsn1 or when mounted/synced)
    surfaces["palimpsest_nemesis"] = read_local_nemesis_snapshot(nemesis_path)
    surfaces["palimpsest_bulk_lake"] = read_local_bulk_lake_metrics(bulk_metrics_path)

    if sec_china_extract_path and sec_china_extract_path.exists():
        raw_sec = sec_china_extract_path.read_bytes()
        sec_doc = json.loads(raw_sec.decode("utf-8"))
        matched = sec_doc.get("china_hk_cayman_matched_count")
        if matched is None:
            matched = sec_doc.get("china_hk_named_companies")
        if matched is None and isinstance(sec_doc.get("companies"), list):
            matched = len(sec_doc["companies"])
        surfaces["hel1_sec_china_issuers"] = {
            "product": "Financial-Collector (hel1 SEC Warehouse)",
            "evidence_class": "observed_sec_edgar",
            "source_host": sec_doc.get("source_host", "hel1"),
            "source_db": sec_doc.get("source_db"),
            "path": str(sec_china_extract_path),
            "sha256": hashlib.sha256(raw_sec).hexdigest(),
            "generated_at": sec_doc.get("extracted_at") or sec_doc.get("generated_at"),
            "total_sec_companies": sec_doc.get("total_sec_companies"),
            "china_hk_cayman_matched_companies": matched,
        }

    available_remote = sum(
        1 for k in ("seiche_money_markets", "seiche_capital_markets", "liquilens_board", "undertow_summary")
        if isinstance(surfaces.get(k), dict) and surfaces[k].get("evidence_class") != "unavailable"
    )
    return {
        "schema": "palimpsest.fleet-data-bridge.v1",
        "generated_at": retrieved_at,
        "status": "ok" if available_remote == 4 else ("partial" if available_remote > 0 else "unavailable"),
        "boundary_note": (
            "Each product retains its own observation clock, knowledge clock, evidence class, "
            "and upstream rights. Cross-product joins are research context, never a merged score."
        ),
        "surfaces": surfaces,
        "errors": errors,
    }
