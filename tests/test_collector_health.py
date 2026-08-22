"""Collector health board: catalog states, no imputed live counts."""
from __future__ import annotations

import json
from pathlib import Path

from processors.collector_health import build_health


def test_empty_catalog_abstains() -> None:
    report = build_health({})
    assert report["abstention"]["code"] == "missing-catalog"
    assert report["signals"] == []


def test_states_are_counted_not_imputed(tmp_path: Path) -> None:
    latest = tmp_path / "readings" / "ddti-latest.json"
    latest.parent.mkdir()
    latest.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-22T09:00:00Z",
                "n_terms": 2,
                "feed_health": {"endpoint": "https://chinadigitaltimes.net/feed/"},
            }
        ),
        encoding="utf-8",
    )
    catalog = {
        "datasets": [
            {
                "id": "ddti",
                "name": "DDTI",
                "status": "live",
                "latest": "readings/ddti-latest.json",
                "artifacts": {
                    "evidence_state": "fresh",
                    "observed_at": "2026-08-22T09:00:00Z",
                    "age_seconds": 10,
                },
            },
            {
                "id": "weibo-hotsearch",
                "name": "Weibo",
                "status": "live",
                "latest": "readings/weibo-hotsearch-latest.json",
                "artifacts": {"evidence_state": "stale", "age_seconds": 200000},
            },
        ]
    }
    report = build_health(catalog, root=tmp_path)
    assert report["summary"]["by_state"]["fresh"] == 1
    assert report["summary"]["by_state"]["stale"] == 1
    assert report["summary"]["n_datasets"] == 2
    ddti = next(row for row in report["signals"] if row["id"] == "ddti")
    assert ddti["envelope_schema"].startswith("palimpsest-collector-artifact")
