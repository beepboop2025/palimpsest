#!/usr/bin/env python3
"""Read-only extractor staging China & BRI corridor Parquet slices from fsn1 bulk.duckdb.

Runs as a bounded, read-only privileged pre-step (``ExecStartPre=+``) so the
main ``palimpsest-china-dark-data.service`` process runs strictly unprivileged
as ``User=palimpsest`` without weakening permissions on ``/var/lib/palimpsest/bulk-data``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

SLICE_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "bulk_wdi_china_bri",
        "palimpsest_wdi_observations",
        """
        SELECT country_code, country_name, indicator_code, indicator_name, value, period
        FROM palimpsest_wdi_observations
        WHERE country_code IN (
            'CHN','HKG','MAC','PAK','LKA','AGO','KEN','ZMB','LAO','ETH',
            'MMR','KHM','BGD','EGY','ARG','ECU','NGA','TZA','UGA','GHA',
            'IDN','VNM','KAZ','UZB','SRB','MNE','DJI'
        )
        """,
    ),
    (
        "bulk_unodc_china_corridor",
        "palimpsest_unodc_reports",
        """
        SELECT seizure_year, seizure_date, country_territory, iso3, region,
               city, administrative_region, drug_substance, measurement_unit,
               quantity_seized, trafficking_mode
        FROM palimpsest_unodc_reports
        WHERE iso3 IN ('CHN','HKG','MAC','MMR','LAO','KHM','THA','VNM','MYS','MEX')
        """,
    ),
    (
        "bulk_ucdp_china_bri_corridor",
        "palimpsest_ucdp_events",
        """
        SELECT id, year, type_of_violence, conflict_name, side_a, side_b,
               country, region, adm_1, best, low, high,
               date_start, date_end, latitude, longitude
        FROM palimpsest_ucdp_events
        WHERE country IN (
            'China','Myanmar (Burma)','Pakistan','Sri Lanka','Laos',
            'Cambodia','Kazakhstan','Ethiopia','Kenya','Djibouti','Taiwan'
        )
        """,
    ),
    (
        "bulk_ofr_funding_stress",
        "ofr_observations",
        """
        SELECT dataset, observed_on, series_id, value
        FROM ofr_observations
        WHERE CAST(observed_on AS VARCHAR) >= '2020-01-01'
        """,
    ),
)


def stage_slices(bulk_db: Path, out_dir: Path) -> dict[str, int]:
    import duckdb  # type: ignore[import-not-found]

    out_dir.mkdir(parents=True, exist_ok=True)
    if not bulk_db.exists():
        return {}
    con = duckdb.connect(str(bulk_db), read_only=True)
    counts: dict[str, int] = {}
    try:
        available = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')"
            ).fetchall()
        }
        for slice_name, source_table, sql in SLICE_QUERIES:
            if source_table not in available:
                continue
            tmp_path = out_dir / f".{slice_name}.parquet.tmp"
            final_path = out_dir / f"{slice_name}.parquet"
            con.execute(
                f"COPY ({sql}) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                [str(tmp_path)],
            )
            cnt = int(con.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(tmp_path)]).fetchone()[0])
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, final_path)
            counts[slice_name] = cnt
    finally:
        con.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage China/BRI Parquet slices from bulk.duckdb")
    parser.add_argument("--bulk-db", type=Path, default=Path("/var/lib/palimpsest/bulk-data/catalog/bulk.duckdb"))
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    counts = stage_slices(args.bulk_db, args.out_dir)
    print(json.dumps({"status": "ok", "slices": counts, "total_rows": sum(counts.values())}))


if __name__ == "__main__":
    main()
