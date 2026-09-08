from __future__ import annotations

import copy
import itertools
import json

import pytest

from collectors.china_mirror_trade import (FLOWS, INDICATORS, SOURCE_GROUP, build_url, digest,
                                          parse_response, source_policy)
from processors.china_mirror_trade import project, publication_source_group
from scripts.china_mirror_trade_pull import export_csv, load_capture, save_capture, reuse_unchanged_cache

CAPTURED = "2026-09-08T15:00:00Z"


def fixture():
    dimensions = {"freq": ["M"], "reporter": ["EU27_2020"], "partner": ["CN"],
                  "product": ["84", "TOTAL"], "flow": ["1", "2"],
                  "indicators": list(INDICATORS), "time": ["2025-06", "2026-06", "2026-10"]}
    doc = {"class": "dataset", "source": "ESTAT", "extension": {"id": "DS-045409"},
           "updated": "2026-08-14T11:00:00+0200", "id": list(dimensions),
           "size": [len(x) for x in dimensions.values()], "dimension": {}, "value": {}}
    for name, values in dimensions.items():
        doc["dimension"][name] = {"category": {"index": {v: i for i, v in enumerate(values)}, "label": {v: v for v in values}}}
    for i, coordinates in enumerate(itertools.product(*dimensions.values())):
        if coordinates[-1] == "2026-10":
            continue
        doc["value"][str(i)] = 100 if coordinates[-1] == "2025-06" else 110
    return doc, build_url("EU27_2020", "CN", ["84", "TOTAL"], "2010-01")


def parsed(doc=None):
    base, url = fixture()
    return parse_response(json.dumps(doc or base).encode(), source_url=url, collected_at=CAPTURED)


def public_doc():
    snapshot, rows = parsed()
    doc = project(rows, [snapshot], generated_at=CAPTURED, failures=[], expected_batches=1)
    doc["history_export"] = {"path": "/readings/china-mirror-trade-history.csv", "sha256": digest(export_csv(rows)), "rows": len(rows), "numeric_cells": doc["coverage"]["numeric_cells"]}
    return doc


def test_sparse_cube_preserves_empty_and_zero_and_converts_mass():
    raw, url = fixture()
    raw["value"].pop("0")
    raw["value"]["1"] = 0
    snapshot, rows = parse_response(json.dumps(raw).encode(), source_url=url, collected_at=CAPTURED)
    assert len(rows) == 8
    by_key = {(r["product"], r["flow"], r["period"]): r for r in rows}
    missing = by_key[("84", FLOWS["1"], "2025-06")]
    zero = by_key[("84", FLOWS["1"], "2026-06")]
    assert missing["value_eur"] is None and missing["value_eur_status"] == "unavailable"
    assert zero["value_eur"] == 0 and zero["value_eur_status"] == "reported"
    assert zero["weight_kg"] == 11000
    assert snapshot["source_updated_at"] == "2026-08-14T09:00:00Z"
    assert all(r["period"] != "2026-10" for r in rows)


@pytest.mark.parametrize("url", [
    "http://ec.europa.eu/anything", "https://ec.europa.eu.evil.test/anything",
    "https://user@ec.europa.eu/anything", "https://ec.europa.eu:443/anything",
    "https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/OTHER",
])
def test_source_allowlist(url):
    with pytest.raises(ValueError):
        source_policy(url)


@pytest.mark.parametrize("args", [("CN", "CN", ["84"]), ("CH", "CN", ["84"]), ("AT", "CN", ["84011000"]), ("DE", "US", ["84"]), ("DE", "CN", ["84", "84"]), ("DE", "CN", ["<script>"])])
def test_unsupported_reporter_or_product_rejected(args):
    with pytest.raises(ValueError):
        build_url(*args)


@pytest.mark.parametrize("change", [
    lambda d: d.update(source="OTHER"),
    lambda d: d.update(updated="2026-12-01T00:00:00Z"),
    lambda d: d.update(updated="2026-08-01T00:00:00"),
    lambda d: d["value"].update({"2": 100}),
    lambda d: d["value"].update({"0": -1}),
    lambda d: d["value"].update({"0": True}),
    lambda d: d["value"].update({"0": float("nan")}),
    lambda d: d["value"].update({"999999": 1}),
    lambda d: d["dimension"]["partner"]["category"].update(index={"PK": 0}),
    lambda d: d["dimension"]["product"]["category"].update(index={"84": 0, "TOTAL": 0}),
])
def test_malformed_source_data_rejected(change):
    doc, _ = fixture()
    change(doc)
    with pytest.raises(ValueError):
        parsed(doc)


def test_source_flags_retained():
    doc, _ = fixture()
    doc["status"] = {"1": "p"}
    _, rows = parsed(doc)
    row = next(r for r in rows if r["product"] == "84" and r["flow"] == FLOWS["1"] and r["period"] == "2026-06")
    assert row["value_eur_flag"] == "p"


def test_yoy_uses_exact_month_not_previous_available_observation():
    snapshot, rows = parsed()
    for row in rows:
        if row["period"] == "2025-06":
            row["period"] = "2025-05"
    doc = project(rows, [snapshot], generated_at=CAPTURED, failures=[], expected_batches=1)
    assert all(s["year_ago"] is None and s["yoy_value_pct"] is None for s in doc["series"])


def test_publication_contract_and_reproducible_changes():
    doc = public_doc()
    assert publication_source_group(doc) == SOURCE_GROUP
    assert all(s["yoy_value_pct"] == 10 for s in doc["series"])
    assert all(s["unit_value_eur_per_kg"] == 0.01 for s in doc["series"])


@pytest.mark.parametrize("change", [
    lambda d: d.update(raw_payload="private"),
    lambda d: d["rights"].update(license="CC0"),
    lambda d: d["source"].update(publisher="China Customs"),
    lambda d: d["series"][0].update(reporter="CN"),
    lambda d: d["series"][0]["latest"].update(value_eur_status="unavailable"),
    lambda d: d["series"][0].update(yoy_value_pct=99),
    lambda d: d["series"][0]["year_ago"].update(period="2025-05"),
    lambda d: d["snapshots"][0].update(raw_sha256="0" * 64),
    lambda d: d["series"][0]["latest"].update(collected_at="2025-01-01T00:00:00Z"),
    lambda d: d["findings"][0].update(evidence_series=["not-real"]),
])
def test_publication_tampering_rejected(change):
    doc = copy.deepcopy(public_doc())
    change(doc)
    with pytest.raises(ValueError):
        publication_source_group(doc)


def test_immutable_capture_keeps_original_clock_and_detects_corruption(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "receipts").mkdir()
    doc, url = fixture()
    raw = json.dumps(doc).encode()
    first, _ = save_capture(tmp_path, raw=raw, source_url=url, collected_at=CAPTURED)
    second, _ = save_capture(tmp_path, raw=raw, source_url=url, collected_at="2026-09-09T15:00:00Z")
    assert first == second
    (tmp_path / "raw" / (first["raw_sha256"] + ".json")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_capture(tmp_path, first["snapshot_id"], url)


def test_zero_weight_does_not_generate_unit_value():
    snapshot, rows = parsed()
    for row in rows:
        row["weight_kg"] = 0
    doc = project(rows, [snapshot], generated_at=CAPTURED, failures=[], expected_batches=1)
    assert all(s["unit_value_eur_per_kg"] is None and s["yoy_weight_pct"] is None for s in doc["series"])


def test_small_update_probe_reuses_only_matching_recent_full_capture():
    snapshot, _ = parsed()
    assert reuse_unchanged_cache([snapshot], snapshot, last_full_check="2026-09-07T15:00:00Z", checked_at=CAPTURED)
    assert not reuse_unchanged_cache([snapshot], snapshot, last_full_check="2026-09-01T15:00:00Z", checked_at=CAPTURED)
    assert not reuse_unchanged_cache([snapshot], snapshot, last_full_check="2026-09-09T15:00:00Z", checked_at=CAPTURED)
    assert not reuse_unchanged_cache([snapshot], snapshot, last_full_check=None, checked_at=CAPTURED)
    assert not reuse_unchanged_cache([snapshot], {**snapshot, "source_updated_at": CAPTURED}, last_full_check="2026-09-07T15:00:00Z", checked_at=CAPTURED)


def test_revised_vintage_keeps_both_immutable_captures(tmp_path):
    for name in ("raw", "receipts"):
        (tmp_path / name).mkdir()
    doc, url = fixture()
    before, _ = save_capture(tmp_path, raw=json.dumps(doc).encode(), source_url=url, collected_at=CAPTURED)
    doc["value"]["1"] += 1
    after, _ = save_capture(tmp_path, raw=json.dumps(doc).encode(), source_url=url, collected_at="2026-09-09T15:00:00Z")
    assert before["snapshot_id"] != after["snapshot_id"]
    assert len(list((tmp_path / "raw").glob("*.json"))) == 2
    assert load_capture(tmp_path, before["snapshot_id"], url)[0] == before


def test_archive_capacity_does_not_replace_existing_evidence(tmp_path, monkeypatch):
    import scripts.china_mirror_trade_pull as pull
    for name in ("raw", "receipts"):
        (tmp_path / name).mkdir()
    doc, url = fixture()
    raw = json.dumps(doc).encode()
    first, _ = save_capture(tmp_path, raw=raw, source_url=url, collected_at=CAPTURED)
    monkeypatch.setattr(pull, "MAX_STORE_BYTES", len(raw))
    doc["value"]["1"] += 1
    with pytest.raises(ValueError, match="capacity"):
        save_capture(tmp_path, raw=json.dumps(doc).encode(), source_url=url, collected_at=CAPTURED)
    assert load_capture(tmp_path, first["snapshot_id"], url)[0] == first


def test_fetch_uses_hardened_transport_and_source_policy(monkeypatch):
    import collectors.china_mirror_trade as collector
    _, url = fixture()
    calls = []
    def fake(url, **kwargs):
        calls.append((url, kwargs))
        return b"{}"
    monkeypatch.setattr(collector, "safe_fetch_bytes", fake)
    assert collector.fetch_bytes(url) == b"{}"
    assert calls[0][1]["max_redirects"] == 0
    assert calls[0][1]["url_policy"] is source_policy


def test_failed_refresh_retains_last_good_clocks(tmp_path, monkeypatch):
    import scripts.china_mirror_trade_pull as pull
    store = tmp_path / "store"
    store.mkdir()
    for name in ("raw", "receipts"):
        (store / name).mkdir()
    doc, url = fixture()
    snapshot, _ = save_capture(store, raw=json.dumps(doc).encode(), source_url=url, collected_at=CAPTURED)
    (store / "index.json").write_text(json.dumps({url: snapshot["snapshot_id"]}))
    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setattr(pull, "jobs", lambda config: [url])
    monkeypatch.setattr(pull, "now", lambda: "2026-09-09T15:00:00Z")
    def fail(url):
        raise TimeoutError("offline")
    monkeypatch.setattr(pull, "fetch_bytes", fail)
    output = tmp_path / "output.json"
    assert pull.main(["--config", str(config), "--store", str(store), "--output", str(output), "--full-refresh"]) == 0
    result = json.loads(output.read_text())
    assert result["status"] == "partial"
    assert result["snapshots"][0] == snapshot
    assert result["collection"]["failures"][0]["retained"] is True
