import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import build_data_catalog as catalog


ROOT = Path(__file__).resolve().parent.parent


def test_catalog_is_unique_bounded_and_machine_discoverable():
    built, jsonld, package = catalog.build_catalog(
        now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    )

    ids = [item["id"] for item in built["datasets"]]
    assert len(ids) >= 30
    assert len(ids) == len(set(ids))
    assert built["summary"]["datasets"] == len(ids)
    assert set(built["summary"]["layers"]) >= {
        "network",
        "content",
        "platform",
        "state",
        "model",
        "cross-layer",
    }
    assert jsonld["@type"] == "DataCatalog"
    assert len(jsonld["dataset"]) == len(ids)
    assert package["profile"] == "data-package"
    assert package["resources"]


def test_catalog_keeps_collection_mode_rights_and_caveats_explicit():
    built, _jsonld, _package = catalog.build_catalog(
        now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    )
    by_id = {item["id"]: item for item in built["datasets"]}

    assert by_id["inside-view"]["collection_mode"] == "active-consented"
    assert by_id["inside-view"]["artifacts"]["evidence_state"] == "gated"
    assert by_id["cloudflare-radar-tcp"]["collection_mode"] == "passive-upstream"
    assert by_id["cloudflare-radar-tcp"]["artifacts"]["evidence_state"] == "gated"
    assert "causal proof" in by_id["cloudflare-radar-tcp"]["description"]
    assert by_id["baike-redaction"]["artifacts"]["evidence_state"] == "disabled"
    assert by_id["ooni-bulk"]["artifacts"]["evidence_state"] == "private-node"
    assert "CC BY-NC-SA" in by_id["ooni-bulk"]["license"]["name"]
    assert "upstream" in by_id["ddti"]["license"]["name"].lower()
    assert "caus" in by_id["cross-layer"]["description"].lower()


def test_ooni_catalog_scope_matches_the_committed_warehouse_allowlist():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    warehouse = json.loads(
        (ROOT / "config" / "ooni_bulk.json").read_text(encoding="utf-8")
    )
    ooni = next(item for item in source["datasets"] if item["id"] == "ooni-bulk")

    assert ooni["geography"] == warehouse["countries"]
    assert ooni["count_fields"] == ["measurements", "objects", "compressed_bytes"]


def test_research_corpus_catalog_matches_allowlist_and_publication_boundary():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    allowlist = json.loads(
        (ROOT / "config" / "research_corpus_sources.json").read_text(encoding="utf-8")
    )
    entry = next(item for item in source["datasets"] if item["id"] == "research-corpus")

    assert entry["sources"] == [item["repository"] for item in allowlist["sources"]]
    assert entry["collection_mode"] == "passive-upstream"
    assert entry["latest"] == "readings/research-corpus-latest.json"
    assert entry["history"] == "readings/research-corpus-history.jsonl"
    assert entry["method"] == "docs/RESEARCH-CORPUS-INGEST.md"
    assert entry["count_fields"] == ["n_sources", "n_changed", "n_unchanged"]
    description = entry["description"].lower()
    assert "without republishing" in description
    assert "keyword lists" in description and "notice bodies" in description


def test_newsroom_catalog_describes_a_deterministic_hourly_publication():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    entry = next(item for item in source["datasets"] if item["id"] == "newsroom")

    assert (entry["layer"], entry["stage"], entry["collection_mode"]) == (
        "cross-layer",
        "publication",
        "deterministic",
    )
    assert entry["cadence"] == "PT1H"
    assert entry["status"] == "live"
    assert entry["latest"] == "readings/newsroom-latest.json"
    assert entry["landing_page"] == "news/"
    assert entry["method"] == "docs/NEWSROOM.md"
    assert entry["count_fields"] == ["n_stories"]
    assert "causal inference" in entry["description"]


def test_evidence_wire_and_economic_pulse_keep_collection_semantics_separate():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in source["datasets"]}

    wire = by_id["newswire"]
    assert (wire["stage"], wire["collection_mode"], wire["cadence"]) == (
        "observation",
        "passive-metadata",
        "PT1H",
    )
    assert wire["latest"] == "readings/newswire-latest.json"
    assert wire["history"] == "readings/newswire-versions.jsonl"
    assert wire["landing_page"] == "news/wire/"
    assert "article bodies" in wire["description"]
    assert "causes" in wire["description"]

    pulse = by_id["china-economic-pulse"]
    assert (pulse["layer"], pulse["stage"], pulse["collection_mode"]) == (
        "economy",
        "synthesis",
        "deterministic-revision-safe",
    )
    assert pulse["latest"] == "readings/china-economic-pulse-latest.json"
    assert pulse["landing_page"] == "news/economy/"
    assert "true GDP" in pulse["description"]
    assert "coverage gates" in pulse["description"]


def test_bleedthrough_catalog_exposes_only_the_sanitized_relay() -> None:
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    entry = next(item for item in source["datasets"] if item["id"] == "bleedthrough")

    assert entry["collection_mode"] == "active-controlled-external-vantage"
    assert entry["latest"] == "readings/bleedthrough-latest.json"
    assert entry["history"] == "readings/bleedthrough-history.jsonl"
    assert entry["method"] == "docs/BLEEDTHROUGH.md"
    assert entry["count_fields"] == [
        "vantages_probed",
        "vantages_injecting",
        "distinct_pools",
        "events",
    ]
    description = entry["description"].lower()
    assert "privacy-minimized" in description
    assert "remain private" in description
    assert "attribution" in description


def test_investigations_catalog_exposes_review_gates_and_public_contract():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    entry = next(item for item in source["datasets"] if item["id"] == "investigations")

    assert (entry["layer"], entry["stage"], entry["collection_mode"]) == (
        "cross-layer",
        "publication",
        "deterministic-review-gated",
    )
    assert entry["latest"] == "readings/investigations-latest.json"
    assert entry["landing_page"] == "news/investigations/"
    assert entry["method"] == "docs/INVESTIGATIONS.md"
    assert entry["count_fields"] == ["n_cases"]
    description = entry["description"].lower()
    assert "counterevidence" in description
    assert "does not automate allegations" in description


def test_reporting_newsroom_catalog_keeps_five_capabilities_distinct():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in source["datasets"]}

    assert by_id["primary-documents"]["collection_mode"] == (
        "passive-primary-document"
    )
    assert by_id["primary-documents"]["stage"] == "archive"
    assert "not a parsed observation" in by_id["primary-documents"]["description"]
    assert by_id["corroboration"]["collection_mode"] == (
        "deterministic-human-reviewed"
    )
    assert "never confirms" in by_id["corroboration"]["description"]
    assert by_id["network-rounds"]["status"] == "warming"
    assert "national censorship percentage" in by_id["network-rounds"]["description"]
    assert by_id["source-workflow"]["status"] == "gated"
    assert "note text stay" in by_id["source-workflow"]["description"]
    assert by_id["editorial-readiness"]["stage"] == "publication"
    assert "never publishes automatically" in by_id["editorial-readiness"]["description"]
    assert {
        by_id[name]["latest"]
        for name in (
            "primary-documents",
            "corroboration",
            "network-rounds",
            "source-workflow",
            "editorial-readiness",
        )
    } == {
        f"readings/{name}-latest.json"
        for name in (
            "primary-documents",
            "corroboration",
            "network-rounds",
            "source-workflow",
            "editorial-readiness",
        )
    }


def test_public_catalog_never_points_into_private_runtime_directories():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    for item in source["datasets"]:
        for field in ("latest", "history", "landing_page", "method"):
            value = item.get(field)
            if value:
                assert not Path(value).is_absolute()
                assert ".." not in Path(value).parts
                assert not value.startswith(("data/", "var/", "warehouse/"))


def test_rejects_escaping_paths():
    with pytest.raises(ValueError, match="stay inside"):
        catalog._safe_repo_path("../private.json")


def test_duration_parser_matches_catalog_subset():
    assert catalog._duration_seconds("PT1H") == 3600
    assert catalog._duration_seconds("P1D") == 86400
    assert catalog._duration_seconds("P1W") == 7 * 86400
    assert catalog._duration_seconds("P1M") == 31 * 86400
    assert catalog._duration_seconds("every minute") is None


def test_count_extraction_is_explicit_and_does_not_walk_evidence_payloads():
    doc = {"country": {"total_tested": 108818}, "events": [1, 2, 3], "secret": 99}
    assert (
        catalog._bounded_count(catalog._value_at(doc, "country.total_tested")) == 108818
    )
    assert catalog._bounded_count(catalog._value_at(doc, "events")) == 3
    assert catalog._value_at(doc, "missing.value") is None


def test_generated_files_match_builder_under_source_date_epoch(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1786459200")
    built, jsonld, package = catalog.build_catalog()
    assert built["generated_at"].endswith("Z")
    assert jsonld["dateModified"] == built["generated_at"]
    assert package["created"] == built["generated_at"]


def test_checked_in_catalog_views_do_not_drift_from_source_and_readings():
    committed = json.loads(
        (ROOT / "readings" / "catalog.json").read_text(encoding="utf-8")
    )
    built_at = catalog._parse_time(committed["generated_at"])
    assert built_at is not None
    built, jsonld, package = catalog.build_catalog(now=built_at)

    assert committed == built
    assert (
        json.loads((ROOT / "readings" / "catalog.jsonld").read_text(encoding="utf-8"))
        == jsonld
    )
    assert (
        json.loads((ROOT / "datapackage.json").read_text(encoding="utf-8")) == package
    )


def test_jsonld_advertises_only_downloads_that_exist():
    _built, jsonld, _package = catalog.build_catalog(
        now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    )
    for dataset in jsonld["dataset"]:
        for distribution in dataset.get("distribution", []):
            url = distribution["contentUrl"]
            relative = url.removeprefix(catalog.SITE)
            assert (ROOT / relative).is_file(), url


def test_catalog_page_and_assets_exist():
    assert (ROOT / "data.html").is_file()
    assert (ROOT / "assets" / "data-catalog.css").is_file()
    assert (ROOT / "assets" / "data-catalog.js").is_file()


def test_hourly_rollup_rebuilds_and_commits_catalog_views():
    workflow = (ROOT / ".github" / "workflows" / "osint-china-refresh.yml").read_text(
        encoding="utf-8"
    )
    assert workflow.count("python -m scripts.build_data_catalog") == 3
    committed_lines = {
        line.strip().rstrip("\\").strip()
        for line in workflow.splitlines()
        if "catalog.json" in line or "datapackage.json" in line
    }
    assert committed_lines == {
        "readings/catalog.json",
        "readings/catalog.jsonld",
        "datapackage.json",
    }
    for output in committed_lines:
        assert (
            sum(
                line.strip().rstrip("\\").strip() == output
                for line in workflow.splitlines()
            )
            == 3
        )
