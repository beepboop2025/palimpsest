"""Refresh a supplemental, attributed country-level debt and welfare dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from collectors.bri_world_bank_wdi import acquisition_receipt_for, build_url, fetch_bytes, load_registry, parse_response, verify_acquisition_receipt
from core.bri_observation import canonical_json_bytes
from scripts.china_economic_health_pull import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def project(collection) -> dict:
    bundle = collection.to_dict()
    grouped = {}
    for row in bundle["observations"]:
        grouped.setdefault((row["country_code"], row["indicator_id"]), []).append(row)
    series = []
    for (country, indicator), rows in sorted(grouped.items()):
        rows.sort(key=lambda row: row["period_end"])
        available = [r for r in rows if r["evidence_state"] == "observed"]
        binding = collection.registry.bindings[indicator]
        series.append({"country_code": country, "indicator_id": indicator, "name": binding.source_title,
                       "unit": binding.unit, "latest_available": available[-1] if available else None,
                       "last_requested_period": {k: rows[-1][k] for k in ("period_end", "value", "evidence_state", "unavailability_reason")},
                       "history": [{k: r[k] for k in ("period_end", "value", "evidence_state", "obs_status", "footnote", "observation_id", "source_row_sha256")} for r in rows],
                       "source_url": f"https://data.worldbank.org/indicator/{indicator}?locations={country}"})
    return {"schema": "palimpsest.regional-economic-context.v1", "generated_at": bundle["generated_at"],
            "coverage": bundle["coverage"], "source": bundle["source"], "context_policy": bundle["context_policy"],
            "registry_sha256": bundle["registry_sha256"], "collection_id": bundle["collection_id"],
            "observations_sha256": bundle["observations_sha256"], "request_receipts": bundle["request_receipts"],
            "series": series,
            "interpretation": "Annual national context. A latest available year can precede the requested end year. These are not CPEC project accounts, Balochistan district statistics, bilateral Chinese debt totals or current monthly readings."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path(os.environ.get("PALIMPSEST_REGIONAL_ECONOMIC_STORE", ROOT / "data/review/regional-economics")))
    parser.add_argument("--output", type=Path, default=ROOT / "readings/regional-economic-context-latest.json")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    registry = load_registry(ROOT / "config/regional_economic_series.json")
    end_year = datetime.now(timezone.utc).year - 1
    url = build_url(registry, start_year=2000, end_year=end_year)
    if args.input:
        if not args.receipt:
            raise ValueError("saved response requires its acquisition receipt")
        raw = args.input.read_bytes()
        receipt = verify_acquisition_receipt(args.receipt.read_bytes(), raw=raw, expected_url=url)
    else:
        raw = fetch_bytes(url)
        receipt = acquisition_receipt_for(raw, evidence_url=url, retrieved_at=datetime.now(timezone.utc))
    collection = parse_response(raw, registry=registry, evidence_url=url, start_year=2000,
                                end_year=end_year, retrieved_at=receipt.retrieved_at)
    args.store.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(raw).hexdigest()
    raw_path = args.store / (identity + ".json")
    if raw_path.exists() and raw_path.read_bytes() != raw:
        raise ValueError("immutable World Bank raw evidence changed")
    if not raw_path.exists():
        raw_path.write_bytes(raw)
    receipt_raw = canonical_json_bytes(receipt.to_dict())
    receipt_path = args.store / (hashlib.sha256(receipt_raw).hexdigest() + ".receipt.json")
    if not receipt_path.exists():
        receipt_path.write_bytes(receipt_raw)
    atomic_json(args.output, project(collection))
    print(json.dumps(collection.to_dict()["coverage"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
