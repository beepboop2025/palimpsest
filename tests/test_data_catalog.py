import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import build_data_catalog as catalog
from scripts import anchor_roots


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


def test_gated_datasets_do_not_advertise_legacy_files_as_public_distributions():
    built, jsonld, package = catalog.build_catalog(
        now=datetime(2026, 8, 26, tzinfo=timezone.utc)
    )
    gated_ids = {
        item["id"] for item in built["datasets"] if item["status"] == "gated"
    }
    assert {
        "china-economic-observations",
        "china-economic-pulse",
        "cny-fix-gap",
        "data-darkness",
        "evidence-mesh",
        "machine-investigations",
    } <= gated_ids
    for item in built["datasets"]:
        if item["id"] not in gated_ids:
            continue
        assert item["artifacts"] == {
            "evidence_state": "gated",
            "observed_at": None,
            "age_seconds": None,
            "counts": {},
            "latest_bytes": None,
            "history_bytes": None,
            "history_rows": None,
            "latest_available": False,
            "history_available": False,
        }
    by_id = {item["identifier"]: item for item in jsonld["dataset"]}
    assert all(by_id[item_id]["distribution"] == [] for item_id in gated_ids)
    resource_names = {resource["name"] for resource in package["resources"]}
    assert all(
        not any(name.startswith(f"{item_id}-") for name in resource_names)
        for item_id in gated_ids
    )


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
    assurance = by_id["eval-assurance"]
    assert assurance["urls"]["latest"].endswith("/readings/eval-assurance-latest.json")
    assert assurance["method"] == "docs/EVAL-ASSURANCE.md"
    assert "claim ceiling" in assurance["description"].lower()
    journal = by_id["eval-journal"]
    assert journal["urls"]["latest"].endswith("/readings/eval-journal-latest.json")
    assert journal["landing_page"] == "evals/"
    assert "falsifier" in journal["description"].lower()
    transcripts = by_id["gfi-transcripts"]
    assert transcripts["latest"] == "readings/gfi-transcripts-latest.json"
    assert transcripts["method"] == "readings/gfi-evaluation-protocol-v2.json"
    assert transcripts["count_fields"] == [
        "n_models", "n_prompt_arms", "samples_per_cell", "n_cells", "n_samples"
    ]
    assert "sealed" in transcripts["description"].lower()


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
    assert pulse["status"] == "gated"
    assert pulse["landing_page"] == "news/economy/"
    assert "true GDP" in pulse["description"]
    assert "coverage gates" in pulse["description"]


def test_social_ledger_catalog_keeps_access_and_corroboration_boundaries_explicit():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    entry = next(
        item for item in source["datasets"] if item["id"] == "social-observations"
    )

    assert (entry["layer"], entry["stage"], entry["collection_mode"]) == (
        "narrative",
        "observation",
        "official-api-bounded-metadata",
    )
    assert entry["status"] == "gated"
    assert entry["cadence"] == "PT1H"
    assert entry["latest"] == "readings/social-observations-latest.json"
    assert entry["history"] == "readings/social-observations-versions.jsonl"
    assert entry["landing_page"] == "news/china/situation/"
    assert entry["method"] == "docs/SOCIAL-OBSERVATION-PIPELINE.md"
    assert entry["count_fields"] == [
        "n_observations",
        "coverage.configured",
        "coverage.successful",
        "coverage.failed",
    ]
    description = entry["description"].lower()
    assert "context, not corroboration" in description
    assert "credentials" in description and "direct messages" in description


def test_china_situation_catalog_exposes_layer_specific_coverage_and_public_surface():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    entry = next(
        item for item in source["datasets"] if item["id"] == "china-situation"
    )

    assert (entry["layer"], entry["stage"], entry["collection_mode"]) == (
        "cross-layer",
        "synthesis",
        "deterministic-evidence-bound-projection",
    )
    assert entry["status"] == "gated"
    assert entry["cadence"] == "PT1H"
    assert entry["latest"] == "readings/china-situation-latest.json"
    assert entry["landing_page"] == "news/china/situation/"
    assert entry["method"] == "docs/SOCIAL-OBSERVATION-PIPELINE.md"
    assert entry["count_fields"] == [
        "coverage.in_scope_events",
        "coverage.publisher_reports",
        "coverage.measurement_context_rows",
        "coverage.social_observations_linked",
        "coverage.reviewed_telegram_signals",
    ]
    assert entry["sources"] == [
        "Palimpsest Evidence Wire",
        "Bounded Social Observation Ledger",
        "Dragon Whispers",
        "Palimpsest Observatory event analysis",
    ]
    description = entry["description"].lower()
    assert "exact url joins" in description
    assert "never become article verification" in description

    built, _jsonld, _package = catalog.build_catalog(
        now=datetime(2026, 8, 16, 16, tzinfo=timezone.utc)
    )
    built_entry = next(
        item for item in built["datasets"] if item["id"] == "china-situation"
    )
    assert built_entry["urls"]["latest"] == (
        "https://palimpsest.info/readings/china-situation-latest.json"
    )
    assert built_entry["urls"]["landing_page"] == (
        "https://palimpsest.info/news/china/situation/"
    )


def test_economic_observation_ledger_is_a_first_class_bitemporal_distribution():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in source["datasets"]}

    telemetry = by_id["china-econ"]
    assert telemetry["cadence"] == "PT6H"
    assert telemetry["status"] == "gated"

    ledger = by_id["china-economic-observations"]
    assert (ledger["layer"], ledger["stage"], ledger["collection_mode"]) == (
        "economy",
        "observation",
        "passive-bitemporal-aggregate",
    )
    assert ledger["latest"] == "readings/china-econ-observations-latest.json"
    assert ledger["history"] == "readings/china-econ-observations.jsonl"
    assert ledger["landing_page"] == "china/"
    assert ledger["cadence"] == "P1D"
    assert ledger["status"] == "gated"
    assert ledger["freshness_budget"] == "P10D"
    assert "collector" in ledger["freshness_semantics"]
    assert ledger["sources"] == ["CFETS/ChinaMoney"]
    assert ledger["license"]["url"] == "https://palimpsest.info/data.html#rights"
    caveat = ledger["description"].lower()
    assert "aggregate-only" in caveat
    assert "narrow" in caveat
    assert "true gdp" in caveat

    wdi = by_id["china-economic-wdi-history"]
    assert (wdi["layer"], wdi["stage"], wdi["collection_mode"]) == (
        "economy",
        "observation",
        "passive-bitemporal-aggregate",
    )
    assert wdi["latest"] == "readings/china-econ-wdi-latest.json"
    assert wdi["history"] == "readings/china-econ-wdi-observations.jsonl"
    assert wdi["landing_page"] == "china/sources/world_bank_wdi/"
    assert wdi["cadence"] == "P7D"
    assert wdi["freshness_budget"] == "P120D"
    assert wdi["sources"] == ["World Bank World Development Indicators"]
    assert wdi["license"] == {
        "name": "World Bank WDI, CC BY 4.0; attribution required",
        "url": "https://datacatalog.worldbank.org/public-licenses",
    }
    assert "context-only" in wdi["description"]
    assert "null availability" in wdi["freshness_semantics"]

    forecast = by_id["china-economic-forecast"]
    assert forecast["latest"] == "readings/china-econ-forecast-latest.json"
    assert forecast["landing_page"] == "china/"
    assert forecast["cadence"] == "P1D"
    assert forecast["freshness_budget"] == "P10D"
    assert forecast["count_fields"] == [
        "n_targets",
        "summary.ready_targets",
        "summary.abstaining_targets",
    ]
    assert "stay null" in forecast["description"]


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


def test_cite_layer_datasets_are_in_the_atlas():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in source["datasets"]}
    weekly = by_id["weekly-situation"]
    assert weekly["latest"] == "readings/weekly-situation-latest.json"
    assert weekly["landing_page"] == "weekly-situation.html"
    assert weekly["method"] == "docs/WEEKLY-SITUATION.md"
    assert weekly["collection_mode"] == "derived"
    health = by_id["collector-health"]
    assert health["latest"] == "readings/collector-health-latest.json"
    assert health["landing_page"] == "status.html"
    phylo = by_id["gazetteer-phylogeny"]
    assert phylo["latest"] == "readings/gazetteer-phylogeny-latest.json"
    assert phylo["count_fields"] == ["n_nodes", "n_edges"]


def test_machine_analysis_catalog_exposes_mesh_and_abstention_boundary():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in source["datasets"]}

    mesh = by_id["evidence-mesh"]
    assert (mesh["stage"], mesh["collection_mode"], mesh["cadence"]) == (
        "provenance",
        "deterministic-pinned-inputs",
        "PT1H",
    )
    assert mesh["latest"] == "readings/evidence-mesh-latest.json"
    assert mesh["status"] == "gated"
    assert "partner artifacts" in mesh["description"]
    assert "automatically admitted" in mesh["description"]

    machine = by_id["machine-investigations"]
    assert (machine["stage"], machine["collection_mode"], machine["cadence"]) == (
        "publication",
        "deterministic-machine-analysis-gated",
        "PT1H",
    )
    assert machine["latest"] == "readings/machine-investigations-latest.json"
    assert machine["status"] == "gated"
    assert machine["landing_page"] == "news/analysis/"
    assert machine["count_fields"] == ["n_cases"]
    description = machine["description"].lower()
    assert "sentence-level citations" in description
    assert "no human interviews" in description
    assert "abstentionreport" in description


def test_china_censorship_analysis_catalog_exposes_article_quality_boundary():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    entry = next(
        item for item in source["datasets"]
        if item["id"] == "china-censorship-analysis"
    )

    assert entry["collection_mode"] == "deterministic-cross-instrument-analysis"
    assert entry["latest"] == "readings/china-censorship-analysis-latest.json"
    assert entry["landing_page"] == "news/china/analysis/"
    assert entry["method"] == "docs/CHINA-CENSORSHIP-ANALYSIS.md"
    assert entry["count_fields"] == [
        "publication_receipt.required_signal_count",
        "publication_receipt.live_signal_count",
    ]
    description = entry["description"].lower()
    assert "every analytical sentence" in description
    assert "denominators remain separate" in description
    assert "availability warnings" in description


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


def test_explicit_freshness_budget_requires_bounded_semantics():
    source = json.loads(catalog.CONFIG.read_text(encoding="utf-8"))
    target = next(
        item for item in source["datasets"]
        if item["id"] == "china-economic-observations"
    )
    target.pop("freshness_semantics")
    with pytest.raises(ValueError, match="must declare .* together"):
        catalog._validate(source)

    target["freshness_semantics"] = " "
    with pytest.raises(ValueError, match="invalid freshness_semantics"):
        catalog._validate(source)

    target["freshness_semantics"] = "Event-time policy."
    target["freshness_budget"] = "P0D"
    with pytest.raises(ValueError, match="unsupported freshness_budget"):
        catalog._validate(source)


def test_count_extraction_is_explicit_and_does_not_walk_evidence_payloads():
    doc = {"country": {"total_tested": 108818}, "events": [1, 2, 3], "secret": 99}
    assert (
        catalog._bounded_count(catalog._value_at(doc, "country.total_tested")) == 108818
    )
    assert catalog._bounded_count(catalog._value_at(doc, "events")) == 3
    assert catalog._value_at(doc, "missing.value") is None


def test_anchor_metadata_replays_the_summary_and_log_prefix_at_its_clock(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(catalog, "ROOT", tmp_path)
    readings = tmp_path / "readings"
    readings.mkdir()

    def record(ts: str, character: str) -> dict:
        return {
            "ts": ts,
            "roots": {
                "registry_root": character * 64,
                "erasure_root": character * 64,
                "readings_root": character * 64,
                "readings_problems": [],
            },
            "wayback": [{"ok": True, "snapshot": f"https://example/{character}"}],
            "ots": {"ok": True, "proof": f"{character}.txt.ots"},
        }

    historical = record("2026-08-04T11:00:00Z", "a")
    future = record("2026-08-04T12:30:00Z", "b")
    historical_line = (json.dumps(historical) + "\n").encode()
    (readings / "anchors.jsonl").write_bytes(
        historical_line + (json.dumps(future) + "\n").encode()
    )
    (readings / "anchors-latest.json").write_text(
        anchor_roots.serialize_anchor_summary(future), encoding="utf-8"
    )

    metadata = catalog._artifact_metadata({
        "id": "anchors",
        "latest": "readings/anchors-latest.json",
        "history": "readings/anchors.jsonl",
        "cadence": "PT6H",
        "status": "live",
        "count_fields": ["wayback_snapshots"],
    }, now=datetime(2026, 8, 4, 12, tzinfo=timezone.utc))

    expected_latest = anchor_roots.serialize_anchor_summary(historical).encode()
    assert metadata["observed_at"] == "2026-08-04T11:00:00Z"
    assert metadata["latest_bytes"] == len(expected_latest)
    assert metadata["history_bytes"] == len(historical_line)
    assert metadata["history_rows"] == 1
    assert metadata["counts"] == {"wayback_snapshots": 1}


def test_generated_files_match_builder_under_source_date_epoch(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1786459200")
    built, jsonld, package = catalog.build_catalog()
    assert built["generated_at"].endswith("Z")
    assert jsonld["dateModified"] == built["generated_at"]
    assert package["created"] == built["generated_at"]


def test_cli_now_replays_an_exact_timezone_aware_catalog_clock(monkeypatch, capsys):
    seen = {}

    def fake_build_catalog(*, now=None):
        seen["now"] = now
        summary = {"datasets": 0, "published_bytes": 0, "history_rows": 0}
        return ({"summary": summary}, {}, {})

    monkeypatch.setattr(catalog, "build_catalog", fake_build_catalog)
    assert catalog.main(["--check", "--now", "2026-08-12T16:14:02.289540Z"]) == 0
    assert seen["now"] == datetime(
        2026, 8, 12, 16, 14, 2, 289540, tzinfo=timezone.utc
    )
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_cli_now_rejects_an_ambient_timezone(monkeypatch):
    with pytest.raises(SystemExit):
        catalog.main(["--check", "--now", "2026-08-12T16:14:02"])


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
    script = (ROOT / "assets" / "data-catalog.js").read_text(encoding="utf-8")
    assert 'item.artifacts.history_available' in script
    assert ': "Unavailable"' in script


def test_hourly_rollup_rebuilds_and_commits_catalog_views():
    workflow = (ROOT / ".github" / "workflows" / "osint-china-v2-refresh.yml").read_text(
        encoding="utf-8"
    )
    assert workflow.count("python -m scripts.build_data_catalog") == 3
    committed_lines = {
        line.strip().rstrip("\\").strip()
        for line in workflow.splitlines()
        if ("catalog.json" in line or "datapackage.json" in line)
        and "git add -A" not in line
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
