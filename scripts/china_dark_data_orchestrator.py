#!/usr/bin/env python3
"""Self-sustaining Palimpsest China Dark Data Warehouse orchestrator.

Runs all rights-cleared China Dark Data layers + Fleet Data Bridge, enforces
disk-free reserves and SHA-256 provenance receipts, writes partitioned Parquet
tables + a unified DuckDB analytical warehouse, and publishes a 100% CFETS-free
public China Dark Data Observatory summary JSON.

Usage:
    python3 -m scripts.china_dark_data_orchestrator \
        --warehouse-dir /Users/mrinal/FastWorkspace/products/palimpsest-china-dark-data-20261122 \
        --portwatch-ports 35 \
        --portwatch-start-year 2022 \
        --gdelt-intervals 8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow running both as module (`python -m scripts.china_dark_data_orchestrator`)
# and directly (`python scripts/china_dark_data_orchestrator.py`).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from collectors.china_dark_data_bis import collect_bis_china
from collectors.china_energy_macro_dark import collect_china_energy_macro_dark
from collectors.china_gdelt_subnational import (
    collect_common_crawl_index_catalog,
    collect_gdelt_china_subnational,
)
from collectors.china_portwatch_ais import collect_china_portwatch
from collectors.china_sanctions_entities import collect_china_sanctions_and_entities
from core.fleet_data_bridge import (
    DEFAULT_BULK_METRICS,
    DEFAULT_NEMESIS_SNAPSHOT,
    collect_fleet_bridge_snapshot,
)
from core.partitioned_ledger import check_disk_reserve

log = logging.getLogger(__name__)

WAREHOUSE_SCHEMA = "palimpsest.china-dark-data-warehouse.v1"
PUBLIC_OBSERVATORY_SCHEMA = "palimpsest.china-open-dark-data-observatory.v1"
MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024

# Hard fail-closed guard: CFETS/ChinaMoney denied terms must never enter the
# rights-cleared public China Dark Data Observatory JSON.
_FORBIDDEN_CFETS_MARKERS = (
    "cfets_benchmarks",
    "chinamoney.com.cn",
    "shibor_on",
    "fdr007",
    "usdcny_parity",
)


def _atomic_write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(raw).hexdigest()


def _assert_cfets_free(payload: object, label: str) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    for marker in _FORBIDDEN_CFETS_MARKERS:
        if marker in serialized:
            raise RuntimeError(f"Rights isolation violation in {label}: found forbidden marker {marker!r}")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in rows]
    raw = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return len(rows), hashlib.sha256(raw).hexdigest()


def materialize_duckdb_and_parquet(
    warehouse_dir: Path,
    datasets: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Write every collected dataset to JSONL, Parquet, and a unified DuckDB database."""
    jsonl_dir = warehouse_dir / "jsonl"
    parquet_dir = warehouse_dir / "parquet"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    parquet_dir.mkdir(parents=True, exist_ok=True)

    table_stats: dict[str, dict[str, Any]] = {}
    written_jsonl: dict[str, Path] = {}
    for table_name, rows in datasets.items():
        jpath = jsonl_dir / f"{table_name}.jsonl"
        count, sha = _write_jsonl(jpath, rows)
        written_jsonl[table_name] = jpath
        table_stats[table_name] = {
            "rows": count,
            "jsonl_path": str(jpath),
            "jsonl_bytes": jpath.stat().st_size,
            "jsonl_sha256": sha,
            "parquet_path": None,
            "parquet_bytes": 0,
        }

    duckdb_path = warehouse_dir / "palimpsest_china_dark_data.duckdb"
    duckdb_available = False
    try:
        import duckdb  # type: ignore[import-not-found]

        duckdb_available = True
        con = duckdb.connect(str(duckdb_path))
        try:
            for table_name, jpath in written_jsonl.items():
                if table_stats[table_name]["rows"] == 0:
                    continue
                ppath = parquet_dir / f"{table_name}.parquet"
                safe_t = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in table_name)
                con.execute(
                    f"CREATE OR REPLACE TABLE {safe_t} AS "
                    f"SELECT * FROM read_json_auto(?, maximum_object_size=33554432)",
                    [str(jpath)],
                )
                con.execute(
                    f"COPY {safe_t} TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                    [str(ppath)],
                )
                table_stats[table_name]["parquet_path"] = str(ppath)
                table_stats[table_name]["parquet_bytes"] = ppath.stat().st_size
                table_stats[table_name]["parquet_sha256"] = hashlib.sha256(ppath.read_bytes()).hexdigest()
        finally:
            con.close()
    except ImportError:
        log.info("duckdb not installed in current interpreter; JSONL warehouse materialized without Parquet/DuckDB")

    return {
        "duckdb_available": duckdb_available,
        "duckdb_path": str(duckdb_path) if duckdb_available else None,
        "duckdb_bytes": duckdb_path.stat().st_size if duckdb_available and duckdb_path.exists() else 0,
        "tables": table_stats,
        "total_rows": sum(s["rows"] for s in table_stats.values()),
        "total_parquet_bytes": sum(s["parquet_bytes"] for s in table_stats.values()),
        "total_jsonl_bytes": sum(s["jsonl_bytes"] for s in table_stats.values()),
    }


def run_warehouse_refresh(
    warehouse_dir: Path,
    *,
    portwatch_ports: int = 35,
    portwatch_start_year: int = 2022,
    gdelt_intervals: int = 8,
    sec_china_extract_path: Path | None = None,
    nemesis_path: Path = DEFAULT_NEMESIS_SNAPSHOT,
    bulk_metrics_path: Path = DEFAULT_BULK_METRICS,
    min_free_bytes: int = MIN_FREE_BYTES,
    now: str | None = None,
) -> dict[str, Any]:
    """Execute one full, self-sustaining China Dark Data Warehouse refresh."""
    generated_at = now or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    disk_avail = check_disk_reserve(warehouse_dir, min_free_bytes=min_free_bytes)

    raw_layers: dict[str, Any] = {}
    layer_errors: list[dict[str, str]] = []

    def _run(name: str, fn) -> Any:
        try:
            res = fn()
            raw_layers[name] = res
            return res
        except Exception as exc:  # noqa: BLE001
            log.exception("Layer %s failed", name)
            layer_errors.append({"layer": name, "error": f"{type(exc).__name__}: {exc}"})
            raw_layers[name] = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
            return raw_layers[name]

    bis = _run("bis_china_credit", lambda: collect_bis_china(now=generated_at))
    portwatch = _run(
        "imf_portwatch_ais",
        lambda: collect_china_portwatch(
            max_ports=portwatch_ports,
            start_year=portwatch_start_year,
            include_chokepoints=True,
            now=generated_at,
        ),
    )
    sanctions = _run("china_sanctions_entities", lambda: collect_china_sanctions_and_entities(now=generated_at))
    energy_macro = _run("china_energy_macro_dark", lambda: collect_china_energy_macro_dark(now=generated_at))
    gdelt = _run(
        "gdelt_china_subnational",
        lambda: collect_gdelt_china_subnational(num_intervals=gdelt_intervals, now=generated_at),
    )
    cc_watch = _run("common_crawl_index_watch", lambda: collect_common_crawl_index_catalog(now=generated_at))
    fleet = _run(
        "fleet_data_bridge",
        lambda: collect_fleet_bridge_snapshot(
            nemesis_path=nemesis_path,
            bulk_metrics_path=bulk_metrics_path,
            sec_china_extract_path=sec_china_extract_path,
            now=generated_at,
        ),
    )

    # Flatten each layer into queryable analytical tables
    datasets: dict[str, list[dict[str, Any]]] = {
        "bis_china_observations": list(bis.get("observations") or []) if isinstance(bis, dict) else [],
        "bis_shadow_credit_derived": list((bis.get("derived") or {}).get("non_bank_shadow_credit_series") or []) if isinstance(bis, dict) else [],
        "bis_offshore_spv_bonds_derived": list((bis.get("derived") or {}).get("offshore_spv_bond_gap_series") or []) if isinstance(bis, dict) else [],
        "portwatch_china_ports_catalog": list(portwatch.get("ports_catalog") or []) if isinstance(portwatch, dict) else [],
        "portwatch_china_daily_ports": list(portwatch.get("daily_port_rows") or []) if isinstance(portwatch, dict) else [],
        "portwatch_china_daily_chokepoints": list(portwatch.get("daily_chokepoint_rows") or []) if isinstance(portwatch, dict) else [],
        "portwatch_china_monthly_aggregates": list(portwatch.get("monthly_aggregates") or []) if isinstance(portwatch, dict) else [],
        "us_ita_csl_china_entities": list(((sanctions.get("us_ita_csl") or {}).get("entities")) or []) if isinstance(sanctions, dict) else [],
        "un_sanctions_china_nexus": list(((sanctions.get("un_sanctions") or {}).get("entries")) or []) if isinstance(sanctions, dict) else [],
        "gleif_china_hk_sample_entities": (
            list(((sanctions.get("gleif_china") or {}).get("sample_entities")) or [])
            + list(((sanctions.get("gleif_hk") or {}).get("sample_entities")) or [])
            if isinstance(sanctions, dict)
            else []
        ),
        "ember_china_monthly_electricity": list(((energy_macro.get("ember_electricity") or {}).get("rows")) or []) if isinstance(energy_macro, dict) else [],
        "imf_china_bri_macro_fiscal": list(((energy_macro.get("imf_datamapper") or {}).get("rows")) or []) if isinstance(energy_macro, dict) else [],
        "wb_ids_bri_sovereign_debt": list(((energy_macro.get("wb_ids_bri_debt") or {}).get("rows")) or []) if isinstance(energy_macro, dict) else [],
        "openalex_china_critical_tech": list(((energy_macro.get("openalex_china_tech") or {}).get("collab_shares")) or []) if isinstance(energy_macro, dict) else [],
        "gdelt_china_subnational_events": list(gdelt.get("events") or []) if isinstance(gdelt, dict) else [],
    }

    if sec_china_extract_path and sec_china_extract_path.exists():
        sec_doc = json.loads(sec_china_extract_path.read_text(encoding="utf-8"))
        datasets["sec_china_hk_issuers"] = list(sec_doc.get("companies") or [])

    storage = materialize_duckdb_and_parquet(warehouse_dir, datasets)

    # Build 100% CFETS-free public China Dark Data Observatory summary
    public_observatory = {
        "schema": PUBLIC_OBSERVATORY_SCHEMA,
        "generated_at": generated_at,
        "rights_isolation": {
            "cfets_excluded": True,
            "policy_basis": "All included layers are public-domain, CC0, CC-BY-4.0, or official free-redistribution datasets with explicit attribution. Denied CFETS/ChinaMoney benchmarks remain strictly segregated.",
        },
        "warehouse_totals": {
            "tables": len(datasets),
            "total_records_materialized": storage["total_rows"],
            "total_parquet_bytes": storage["total_parquet_bytes"],
            "total_jsonl_bytes": storage["total_jsonl_bytes"],
            "duckdb_bytes": storage["duckdb_bytes"],
            "disk_free_bytes": disk_avail,
        },
        "layers": {
            "bis_china_credit": {
                "status": bis.get("status"),
                "summary": bis.get("summary"),
                "source": bis.get("source"),
            },
            "imf_portwatch_ais": {
                "status": portwatch.get("status"),
                "summary": portwatch.get("summary"),
                "source": portwatch.get("source"),
            },
            "china_sanctions_entities": {
                "status": sanctions.get("status"),
                "summary": sanctions.get("summary"),
                "csl_by_source_list": (sanctions.get("us_ita_csl") or {}).get("by_source_list") if isinstance(sanctions, dict) else None,
                "csl_by_jurisdiction": (sanctions.get("us_ita_csl") or {}).get("by_jurisdiction") if isinstance(sanctions, dict) else None,
            },
            "china_energy_macro_dark": {
                "status": energy_macro.get("status"),
                "summary": energy_macro.get("summary"),
                "ember_latest_china": (energy_macro.get("ember_electricity") or {}).get("latest_china_snapshot") if isinstance(energy_macro, dict) else None,
                "imf_latest_china": (energy_macro.get("imf_datamapper") or {}).get("latest_china") if isinstance(energy_macro, dict) else None,
            },
            "gdelt_china_subnational": {
                "status": gdelt.get("status"),
                "summary": gdelt.get("summary"),
                "source": gdelt.get("source"),
            },
            "common_crawl_index_watch": cc_watch,
            "fleet_data_bridge": fleet,
        },
        "layer_errors": layer_errors,
    }

    _assert_cfets_free(public_observatory, "china-open-dark-data-latest.json")
    public_path = warehouse_dir / "china-open-dark-data-latest.json"
    public_sha = _atomic_write_json(public_path, public_observatory)

    manifest = {
        "schema": WAREHOUSE_SCHEMA,
        "generated_at": generated_at,
        "public_observatory_path": str(public_path),
        "public_observatory_sha256": public_sha,
        "storage": storage,
        "layer_errors": layer_errors,
    }
    manifest_path = warehouse_dir / "warehouse-manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return manifest


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Build & refresh Palimpsest China Dark Data Warehouse")
    parser.add_argument("--warehouse-dir", type=Path, required=True)
    parser.add_argument("--portwatch-ports", type=int, default=35)
    parser.add_argument("--portwatch-start-year", type=int, default=2022)
    parser.add_argument("--gdelt-intervals", type=int, default=8)
    parser.add_argument("--sec-china-extract", type=Path, default=None)
    parser.add_argument("--nemesis-path", type=Path, default=DEFAULT_NEMESIS_SNAPSHOT)
    parser.add_argument("--bulk-metrics-path", type=Path, default=DEFAULT_BULK_METRICS)
    args = parser.parse_args()

    manifest = run_warehouse_refresh(
        args.warehouse_dir,
        portwatch_ports=args.portwatch_ports,
        portwatch_start_year=args.portwatch_start_year,
        gdelt_intervals=args.gdelt_intervals,
        sec_china_extract_path=args.sec_china_extract,
        nemesis_path=args.nemesis_path,
        bulk_metrics_path=args.bulk_metrics_path,
    )
    print(json.dumps({
        "status": "ok",
        "generated_at": manifest["generated_at"],
        "total_rows": manifest["storage"]["total_rows"],
        "duckdb_path": manifest["storage"]["duckdb_path"],
        "public_observatory_path": manifest["public_observatory_path"],
        "layer_errors": manifest["layer_errors"],
    }, indent=2))


if __name__ == "__main__":
    main()
