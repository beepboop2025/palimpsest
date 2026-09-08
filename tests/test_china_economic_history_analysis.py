import csv
import hashlib
import io

import pytest

from processors import china_economic_history as history


def row(**overrides):
    return {"family": "pmi", "release_title": "Purchasing Managers Index for August 2026",
            "released_at": "2026-09-01T01:30:00Z", "collected_at": "2026-09-08T01:30:00Z",
            "table": "table-1", "table_context": "China Manufacturing PMI",
            "source_row": "2", "source_column": "2", "row_label": "2025-August",
            "column_label": "Unit: % | PMI", "raw_value": "49.4", "value": "49.4", "status": "observed",
            "source_url": "https://www.stats.gov.cn/english/PressRelease/202609/t20260901_1965170.html",
            "raw_sha256": "a"*64, **overrides}


def build(monkeypatch, rows):
    monkeypatch.setattr(history, "validate", lambda _: None)
    stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=list(row()))
    writer.writeheader(); writer.writerows(rows); raw = stream.getvalue().encode()
    snapshot = {"generated_at": "2026-09-08T01:30:00Z", "history_export": {
        "sha256": hashlib.sha256(raw).hexdigest(), "numeric_cells": sum(r["value"] != "" for r in rows),
        "vintages": len({(r["source_url"], r["raw_sha256"]) for r in rows})}}
    return history.build_history(snapshot, raw)


def test_explicit_year_carries_within_table_and_deduplicates_vintages(monkeypatch):
    rows = [row(), row(row_label="September", source_row="3"), row(row_label="2026-January", source_row="4"), row(row_label="February", source_row="5")]
    result = build(monkeypatch, rows + rows)
    points = result["series"][0]["points"]
    assert [p["period"] for p in points] == ["2025-08", "2025-09", "2026-01", "2026-02"]
    assert all(p["retained_source_vintages"] == 1 for p in points)


def test_year_never_carries_between_tables_or_infers_from_capture_clock(monkeypatch):
    result = build(monkeypatch, [row(), row(table="table-2", row_label="February")])
    assert [p["period"] for p in result["series"][0]["points"]] == ["2025-08"]


def test_latest_vintage_keeps_changed_value_and_revision_receipt(monkeypatch):
    old = row(collected_at="2026-09-02T00:00:00Z")
    new = row(raw_sha256="b"*64, value="48.7", raw_value="48.7")
    result = build(monkeypatch, [old,new])
    point = result["series"][0]["points"][0]
    assert point["value"] == 48.7 and point["distinct_reported_values"] == 2
    assert result["revisions"][0]["changed_cells"] == 1
    assert result["revisions"][0]["before_raw_sha256"] == "a"*64


def test_housing_streak_stops_at_missing_month_and_coverage_is_not_zero(monkeypatch):
    rows = [row(family="housing", release_title=f"Sales Prices in {m} 2026", table_context="Newly Constructed Residential Buildings", row_label="Beijing", column_label="M/M | Last Month=100", raw_value="99.5", value="99.5") for m in ["January","February","April","May","June","July"]]
    result = build(monkeypatch, rows)
    assert result["housing_history"][0]["consecutive_observed_declines"] == 4
    assert "persistence is unavailable" in result["findings"][0]["text"]


def test_conflicting_same_source_month_is_excluded(monkeypatch):
    result = build(monkeypatch, [row(), row(value="48", raw_value="48")])
    assert not result["series"][0]["points"]
    assert result["coverage"]["ambiguous_series_months"] == 1


@pytest.mark.parametrize("change", [{"value":"0", "raw_value":"—"}, {"source_url":"https://evil.example/data"}, {"raw_sha256":"no"}, {"collected_at":"2026-08-01T00:00:00Z"}])
def test_bad_source_or_numeric_projection_rejected(monkeypatch,change):
    with pytest.raises(ValueError): build(monkeypatch,[row(**change)])
