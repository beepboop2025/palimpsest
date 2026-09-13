from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from scripts import build_data_catalog as atlas
from scripts import build_public_data_catalog as public
from scripts import collector_health_pull as health
from processors.collector_health import build_health


NOW = datetime(2026, 9, 13, 18, tzinfo=timezone.utc)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    config = json.loads(atlas.CONFIG.read_text())
    config["datasets"] = [
        row for row in config["datasets"] if row["id"] in {"ddti", "board-alarm"}
    ]
    (tmp_path / "config").mkdir()
    (tmp_path / "readings").mkdir()
    (tmp_path / "config/public_data_catalog.json").write_text(json.dumps(config))
    policy = atlas.ROOT / "config/china_econ_source_policy.json"
    (tmp_path / "config/china_econ_source_policy.json").write_bytes(policy.read_bytes())
    (tmp_path / "readings/ddti-latest.json").write_text(json.dumps({
        "generated_at": "2026-09-13T17:50:00Z", "n_observations": 20,
        "feed_health": {"endpoint": "https://chinadigitaltimes.net/feed/"},
    }))
    (tmp_path / "readings/board-alarm-latest.json").write_text(json.dumps({
        "generated_at": "2026-09-13T17:50:00Z", "shibor_on": 987.654321,
    }))
    (tmp_path / "readings/catalog.json").write_text(json.dumps({
        "datasets": [{"id": "obsolete", "artifacts": {"evidence_state": "fresh"}}],
    }))
    monkeypatch.setattr(atlas, "ROOT", tmp_path)
    monkeypatch.setattr(atlas, "CONFIG", tmp_path / "config/public_data_catalog.json")
    monkeypatch.setattr(atlas, "_utc_now", lambda: NOW)
    return tmp_path


def test_current_artifacts_replace_cached_health_without_advertising_denied_values(snapshot):
    catalog = health._load_catalog()
    rows = {row["id"]: row for row in catalog["datasets"]}
    assert rows["ddti"]["artifacts"]["evidence_state"] == "fresh"
    assert rows["ddti"]["artifacts"]["age_seconds"] == 600
    denied = rows["board-alarm"]["artifacts"]
    assert denied["evidence_state"] == "gated"
    assert denied["latest_available"] is False
    assert denied["counts"] == {}
    assert denied["latest_bytes"] is None
    assert "987.654321" not in json.dumps(catalog)
    report = build_health(catalog, root=snapshot, now=NOW)
    assert report["summary"]["by_state"] == {"fresh": 1, "gated": 1}
    assert {row["id"] for row in report["signals"]} == {"ddti", "board-alarm"}


def test_history_with_denied_values_does_not_hide_a_permitted_latest(snapshot):
    (snapshot / "readings/ddti-history.jsonl").write_text('{"shibor_on": 987.654321}\n')
    rows = public.build_public_catalog(now=NOW)["datasets"]
    row = next(row for row in rows if row["id"] == "ddti")
    assert row["artifacts"]["latest_available"] is True
    assert row["artifacts"]["history_available"] is False
    assert row["artifacts"]["history_rows"] is None


@pytest.mark.parametrize("status,expected", [
    ("abstain", "abstained"), ("partial", "partial"),
    ("permission_required", "gated"),
])
def test_recent_attempt_is_not_mistaken_for_complete_data(snapshot, status, expected):
    path = snapshot / "readings/ddti-latest.json"
    document = json.loads(path.read_text())
    document["status"] = status
    path.write_text(json.dumps(document))
    row = next(row for row in public.build_public_catalog(now=NOW)["datasets"] if row["id"] == "ddti")
    assert row["artifacts"]["evidence_state"] == expected


def test_changed_values_are_still_scanned_for_denied_lineage(snapshot):
    path = snapshot / "readings/ddti-latest.json"
    document = json.loads(path.read_text())
    document["unexpected"] = {"source_id": "cfets_benchmarks", "value": 987.654321}
    path.write_text(json.dumps(document))
    row = next(row for row in public.build_public_catalog(now=NOW)["datasets"] if row["id"] == "ddti")
    assert row["artifacts"]["latest_available"] is False
    assert row["artifacts"]["counts"] == {}


def test_publisher_refreshes_public_catalog_after_all_derived_builds():
    root = Path(__file__).resolve().parents[1]
    source = (root / "ops/railway/palimpsest-railway-publish").read_text()
    assert source.index('"$PYTHON_BIN" -m scripts.build_public_data_catalog') > source.index('wait "$situation_build_pid"')
    assert "/readings/public-data-catalog-latest.json" in (root / "assets/data-catalog.js").read_text()


def test_corrupt_latest_does_not_break_other_datasets(snapshot):
    (snapshot / "readings/ddti-latest.json").write_text("{broken")
    catalog = public.build_public_catalog(now=NOW)
    assert len(catalog["datasets"]) == 2
    assert not next(row for row in catalog["datasets"] if row["id"] == "ddti")["artifacts"]["latest_available"]


def test_corrupt_history_does_not_hide_a_permitted_latest(snapshot):
    (snapshot / "readings/ddti-history.jsonl").write_text("{broken\n")
    row = next(row for row in public.build_public_catalog(now=NOW)["datasets"] if row["id"] == "ddti")
    assert row["artifacts"]["latest_available"] is True
    assert row["artifacts"]["history_available"] is False
