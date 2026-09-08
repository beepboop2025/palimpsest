"""Cross-input publication boundaries and historical-series semantics."""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from pathlib import Path

import pytest

from processors import china_economic_history as history
from processors.china_external_accounts import validate_public
from scripts import build_china_evidence_observatory as observatory
from scripts.stage_pages_rights import _contains_denied_json_value

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://www.stats.gov.cn/english/PressRelease/202607/t20260701_1234567.html"


@pytest.fixture(scope="module")
def inputs():
    names = set(observatory.INPUTS.values()) | {"china-mirror-trade-history.csv"}
    return {"readings/" + name: (ROOT / "readings" / name).read_bytes() for name in names}


class MemoryRoot:
    def __init__(self, files, path=""):
        self.files, self.path = files, path

    def __truediv__(self, child):
        return MemoryRoot(self.files, "/".join(part for part in (self.path, str(child)) if part))

    def read_bytes(self):
        return self.files[self.path]


@pytest.mark.parametrize("location", ["interpretation", "checked_workbooks", "new_captures", "raw_bytes"])
def test_safe_metadata_rejects_private_values_hidden_in_existing_fields(inputs, location):
    doc = json.loads(inputs["readings/china-external-accounts-latest.json"])
    payload = {"observations": [{"source_group": "safe_official_statistics", "value": 123456.78}]}
    if location == "interpretation":
        doc[location] = payload
    elif location in {"checked_workbooks", "new_captures"}:
        doc["collection"][location] = payload
    else:
        doc["sources"][0][location] = payload
    with pytest.raises((ValueError, TypeError)):
        validate_public(doc)
    assert _contains_denied_json_value(doc, denied_source_ids=frozenset({"safe_official_statistics"}), allowed_source_ids=frozenset(), policy_scope=True)


def test_valid_safe_metadata_stages_without_granting_numeric_publication(inputs):
    doc = json.loads(inputs["readings/china-external-accounts-latest.json"])
    validate_public(doc)
    assert not _contains_denied_json_value(doc, denied_source_ids=frozenset({"safe_official_statistics"}), allowed_source_ids=frozenset(), policy_scope=True)
    assert doc["coverage"]["public_numeric_observations"] == 0


def test_combined_builder_binds_mirror_csv_and_rejects_modified_bytes(inputs):
    files = dict(inputs)
    files["readings/china-mirror-trade-history.csv"] += b"\nchanged"
    with pytest.raises(ValueError, match="mirror trade historical bytes"):
        observatory.build(MemoryRoot(files))


def test_combined_builder_keeps_private_numeric_evidence_out_and_escapes_html(inputs):
    data, historical, trade = observatory.build(MemoryRoot(inputs))
    assert data["use_policy"]["private_numeric_data"] == "excluded"
    assert not any(f["evidence_class"] == "safe_official_statistics" for f in data["findings"])
    assert data["input_sha256"]["trade"] == hashlib.sha256(inputs["readings/china-mirror-trade-latest.json"]).hexdigest()
    private = next(d for d in data["datasets"] if d["id"] == "external-accounts")
    assert private["visibility"] == "numeric_data_private" and private["coverage"]["public_numeric_observations"] == 0
    data["findings"][0]["title"] = '<script>alert("untrusted")</script>'
    output = observatory.render(data, historical, trade)
    assert '<script>alert("untrusted")</script>' not in output
    assert '&lt;script&gt;' in output


def row(*, family="pmi", table="table-1", column="PMI", value=49, context="", period="2026-June", raw_sha="a" * 64, captured="2026-07-02T12:00:00Z"):
    return {"family": family, "source_url": SOURCE, "raw_sha256": raw_sha,
            "released_at": "2026-07-01T00:00:00Z", "collected_at": captured,
            "release_title": "Purchasing Managers' Index for June 2026", "table": table,
            "table_context": context, "source_row": "1", "source_column": "5",
            "row_label": period, "column_label": "Unit: % | " + column,
            "raw_value": str(value), "value": str(value), "status": "observed"}


def from_rows(monkeypatch, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader(); writer.writerows(rows)
    raw = stream.getvalue().encode()
    snapshot = {"generated_at": "2026-09-08T12:00:00Z", "history_export": {"sha256": hashlib.sha256(raw).hexdigest(), "numeric_cells": len(rows), "vintages": len({r["raw_sha256"] for r in rows})}}
    # Exercise the archive semantics independently of the separately tested NBS envelope.
    monkeypatch.setattr(history, "validate", lambda document: None)
    return history.build_history(snapshot, raw)


def test_blank_pmi_context_does_not_merge_manufacturing_and_services(monkeypatch):
    manufacturing = ["PMI", "Production Index", "New Order Index", "Raw Materials Inventory Index", "Employment Index", "Supplier Delivery Time Index"]
    services = ["Business Activity Index", "New Order Index", "Input Price Index", "Sales Price Index", "Employment Index", "Business Activity Expectation Index"]
    rows = [row(table="table-1", column=column, value=49) for column in manufacturing]
    rows += [row(table="table-3", column=column, value=47) for column in services]
    result = from_rows(monkeypatch, rows)
    series = {s["id"]: s for s in result["series"]}
    assert "manufacturing:business-activity-index" not in series
    assert series["manufacturing:employment-index"]["points"][0]["value"] == 49
    assert series["nonmanufacturing:employment-index"]["points"][0]["value"] == 47
    assert result["coverage"]["ambiguous_series_months"] == 0


def test_future_reference_month_is_never_an_observed_history_point(monkeypatch):
    try:
        result = from_rows(monkeypatch, [row(period="2027-January", context="Manufacturing PMI")])
    except ValueError:
        return
    assert not any(p["period"] == "2027-01" for s in result["series"] for p in s["points"])


def test_same_url_revision_locator_distinguishes_tables_with_blank_context(monkeypatch):
    rows = [row(family="industrial_profits", table=table, column="Employment Index", value=value,
                raw_sha=sha, captured=captured) for sha, captured, values in (
                    ("a" * 64, "2026-07-02T12:00:00Z", (49, 48)),
                    ("b" * 64, "2026-07-03T12:00:00Z", (47, 48)))
            for table, value in zip(("table-1", "table-3"), values)]
    result = from_rows(monkeypatch, rows)
    assert result["coverage"]["observed_revision_pairs"] == 1
    assert result["revisions"][0]["changed_cells"] == 1


def test_six_month_persistence_without_six_months_is_unavailable():
    housing = [{"kind": "new", "city": "Example", "consecutive_observed_declines": 1,
                "points": [{"period": "2026-07", "value": 99}]}]
    findings = history._findings([], housing)
    assert "unavailable" in findings[0]["text"]
    assert "0 of 0" not in findings[0]["text"]


def test_inventory_heading_translation_preserves_prior_year_comparison(monkeypatch):
    rows = []
    for year, heading, value in (
        (2025, "Turnover Days for Inventory of Finished Goods", 20.5),
        (2026, "Turnover Days of Finished Goods Inventory", 21.3),
    ):
        item = row(family="industrial_profits", column=heading, value=value, period="Total")
        item.update(release_title=f"Industrial Profits in July {year}",
                    released_at=f"{year}-08-27T00:00:00Z",
                    collected_at="2026-09-08T12:00:00Z",
                    column_label=heading + " | Days")
        rows.append(item)
    unrelated = dict(rows[-1], column_label="Turnover Days of Raw Materials Inventory | Days", value="999", raw_value="999")
    result = from_rows(monkeypatch, rows + [unrelated])
    inventory = next(s for s in result["series"] if s["id"] == "inventory-days")
    assert [(p["period"], p["value"]) for p in inventory["points"]] == [("2025-07", 20.5), ("2026-07", 21.3)]
    finding = next(f for f in result["findings"] if f["id"] == "inventory-days")
    assert "+0.8 days from the same month a year earlier" in finding["text"]
    assert finding["evidence"][1]["column_label"] == "Turnover Days for Inventory of Finished Goods | Days"
