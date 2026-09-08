import copy
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from processors.connected_research import build_connected, economic_findings
from scripts import regional_research_pull as wire

ROOT = Path(__file__).resolve().parents[1]


def test_annual_comparisons_preserve_units_and_skip_missing_years():
    series = json.loads((ROOT / "readings/regional-economic-context-latest.json").read_text())["series"]
    row = copy.deepcopy(next(r for r in series if r["indicator_id"] == "FP.CPI.TOTL.ZG"))
    row["history"] = [
        {"period_end": "2022-12-31", "evidence_state": "observed", "value": 10},
        {"period_end": "2023-12-31", "evidence_state": "observed", "value": 6},
        {"period_end": "2024-12-31", "evidence_state": "unavailable", "value": None},
    ]
    finding = economic_findings([row])[0]
    assert finding["change"] == -4
    assert "4.00 percentage points lower" in finding["text"]
    assert "2023" in finding["text"] and "2024" not in finding["text"]
    row["history"][1]["period_end"] = "2024-12-31"
    finding = economic_findings([row])[0]
    assert finding["change"] is None
    assert "No adjacent observed year" in finding["text"]
    assert len(finding["evidence"]) == 1


def test_region_tags_do_not_turn_generic_corridors_into_bri():
    assert not wire.region_tags("Baltoro trekking corridor opens")
    assert "cpec" in wire.region_tags("Gwadar port and CPEC investment")
    assert "balochistan" in wire.region_tags("Balochistan schools face water shortages")


def test_feed_deduplication_retention_and_failed_updates(tmp_path, monkeypatch):
    specs = [s for s in wire.source_specs() if s.id in {"dawn-pakistan", "dawn-business"}]
    assert len(specs) == 2 and specs[0].independence_group == specs[1].independence_group
    date = format_datetime(datetime.now(timezone.utc))
    raw = f'<rss version="2.0"><channel><title>Test</title><item><title>Balochistan water services</title><link>https://www.dawn.com/news/1234567</link><pubDate>{date}</pubDate><description>DO-NOT-RETAIN article description</description></item></channel></rss>'.encode()
    monkeypatch.setattr(wire, "source_specs", lambda: specs)
    monkeypatch.setattr(wire, "safe_fetch_bytes", lambda *args, **kwargs: raw)
    first = wire.collect(tmp_path / "private", tmp_path / "public.json")
    assert len(first["items"]) == 1
    assert set(first["items"][0]["source_ids"]) == {"dawn-pakistan", "dawn-business"}
    assert "DO-NOT-RETAIN" not in (tmp_path / "private/metadata-archive.json").read_text()
    def fail(*args, **kwargs):
        raise ValueError("source offline")
    monkeypatch.setattr(wire, "safe_fetch_bytes", fail)
    second = wire.collect(tmp_path / "private", tmp_path / "public.json")
    assert second["items"] == first["items"]
    assert second["status"] == "partial"
    assert all(source["status"] == "unavailable" for source in second["sources"])


def test_connected_context_keeps_geography_and_evidence_scopes():
    from scripts.build_connected_research import INPUTS
    inputs = {key: json.loads((ROOT / value).read_text()) for key, value in INPUTS.items()}
    result = build_connected(**inputs, input_hashes={key: "a" * 64 for key in inputs})
    baloch = next(r for r in result["regions"] if r["region"] == "balochistan")
    assert {r["country_code"] for r in baloch["national_indicators"]} == {"PAK"}
    assert all(f["scope"] == "annual_country_context" for f in baloch["economic_findings"])
    inputs["economy"]["context_policy"]["aggregate_level"] = "district"
    with pytest.raises(ValueError, match="scope or rights"):
        build_connected(**inputs, input_hashes={})
