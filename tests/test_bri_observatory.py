"""Offline contracts for the Belt and Road evidence backbone."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from processors.bri_observatory import (
    PUBLIC_BUILD_STATES,
    SAFE_PUBLIC_RIGHTS,
    build_public_artifact,
    coverage_report,
    ground_level_priority_adjustment,
    load_registry,
)
from scripts.build_bri_observatory import build


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "bri_observatory.json"
READING = ROOT / "readings" / "belt-and-road-observatory-latest.json"
PAGE = ROOT / "belt-and-road" / "index.html"


def test_registry_is_global_and_has_deep_priority_geographies() -> None:
    registry = load_registry(REGISTRY)
    assert registry["as_of"] == "2026-08-26T12:03:34Z"
    report = coverage_report(registry)
    assert report["source_count"] >= 40
    assert {"official_china", "official_host", "multilateral", "research", "civil_society", "legal", "partner"} <= set(report["source_classes"])
    assert {"GLOBAL", "CHN", "PAK", "MMR", "PAK-BAL", "PAK-GWD", "MMR-RKH"} <= set(report["geographies"])
    assert report["geographies"]["PAK-GWD"]["sources"] >= 5
    assert report["geographies"]["MMR-RKH"]["sources"] >= 5


def test_project_economics_and_ground_level_fields_do_not_flatten_lifecycle() -> None:
    registry = load_registry(REGISTRY)
    policy = registry["publication_policy"]
    assert policy["project_total_rule"] == "never_mix_lifecycle_states"
    assert policy["claim_join_rule"] == "same_project_identity_and_compatible_claim_semantics_only"
    assert {
        "approval_status", "contract_status", "finance_status",
        "implementation_status", "completion_status", "operating_status",
        "committed_amount", "disbursed_amount", "outstanding_amount",
        "price_basis", "sovereign_guarantee_status", "revision",
    } <= set(registry["project_fields"])
    assert {
        "commitment", "disbursement", "debt_service", "fiscal_exposure",
        "port_throughput", "freight_time", "jobs", "distributional_effect",
    } <= set(registry["economic_metrics"])
    assert {
        "jobs_promised", "jobs_observed", "land", "compensation", "fisheries",
        "water", "grievances", "community_reported_benefit",
        "community_reported_harm",
    } <= set(registry["local_impact_fields"])


def test_balochistan_umbrella_never_becomes_one_actor_or_militancy_label() -> None:
    taxonomy = load_registry(REGISTRY)["movement_taxonomy"]
    assert taxonomy["umbrella_term_policy"] == "concept_only_never_single_actor"
    lanes = {lane["lane_id"]: lane for lane in taxonomy["lanes"]}
    assert {
        "electoral_politics", "peaceful_civic_advocacy", "armed_organizations",
        "state_actions", "legal_designations", "rights_and_humanitarian",
        "political_economy_and_local_impact",
    } <= set(lanes)
    assert "peaceful_civic_advocacy" in lanes["armed_organizations"]["prohibited_merges"]
    assert "economic_grievance_as_armed_affiliation" in lanes["political_economy_and_local_impact"]["prohibited_merges"]


def test_rights_gate_blocks_licensed_or_uncleared_inputs_from_build_ready_state() -> None:
    registry = load_registry(REGISTRY)
    for source in registry["sources"]:
        if source["implementation"] in PUBLIC_BUILD_STATES:
            assert source["rights_status"] in SAFE_PUBLIC_RIGHTS
        if source["access_mode"] in {"licensed", "restricted"}:
            assert source["implementation"] in {"blocked", "out_of_scope"}
    acled = next(source for source in registry["sources"] if source["source_id"] == "acled_events")
    assert acled["implementation"] == "blocked"
    assert acled["rights_status"] == "licensed_no_redistribution"


def test_administrative_designations_allegations_and_legal_status_stay_distinct() -> None:
    sources = {source["source_id"]: source for source in load_registry(REGISTRY)["sources"]}
    assert sources["nacta_proscribed"]["claim_classes"] == ["administrative_action"]
    assert sources["uk_proscription"]["claim_classes"] == ["legal_status"]
    assert sources["us_federal_register_bla"]["claim_classes"] == ["legal_status"]
    assert sources["ohchr_balochistan"]["claim_classes"] == ["allegation", "reported_event"]
    assert "not findings" in sources["ohchr_balochistan"]["notes"]


def test_narcoscope_bridge_is_production_verified_and_cannot_infer_actors() -> None:
    registry = load_registry(REGISTRY)
    pin = json.loads(
        (ROOT / "integrations" / "intelligence-commons" / "narcoscope-corridors-pin-v2.json").read_text(
            encoding="utf-8"
        )
    )
    [bridge] = registry["partner_bridges"]
    assert bridge["contract"] == "narcoscope.palimpsest.corridor-aggregate.v2"
    assert bridge["status"] == "production_verified"
    assert bridge["join_policy"] == "geography_and_time_only"
    assert bridge["actor_inference"] == "prohibited"
    source = next(source for source in registry["sources"] if source["source_id"] == "narcoscope_corridors_v2")
    assert source["implementation"] == "live"
    assert "5bf6a31cfd98e56dadca495f35b99ecb73c1d74f" in source["notes"]
    assert registry["as_of"] >= pin["deployment"]["verified_at"]


def test_generated_artifact_and_page_are_exact_and_schema_valid() -> None:
    expected_json, expected_html = build(REGISTRY)
    assert READING.read_bytes() == expected_json
    assert PAGE.read_bytes() == expected_html
    artifact = json.loads(expected_json)
    schema = json.loads((ROOT / "protocol" / "belt-and-road-observatory-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(artifact)
    assert artifact == build_public_artifact(load_registry(REGISTRY))


def test_schema_allows_a_future_fully_covered_registry() -> None:
    artifact = build_public_artifact(load_registry(REGISTRY))
    artifact["coverage_report"]["build_ready_gaps"] = []
    schema = json.loads((ROOT / "protocol" / "belt-and-road-observatory-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(artifact)


def test_ground_level_priority_is_explicit_and_cannot_change_claim_status() -> None:
    registry = load_registry(REGISTRY)
    source = next(item for item in registry["sources"] if item["source_id"] == "balochistan_pnd")
    assert ground_level_priority_adjustment(source) == 6.75
    row = next(item for item in build_public_artifact(registry)["prioritized_backlog"] if item["source_id"] == "balochistan_pnd")
    assert row["ground_level_adjustment"] == 6.75
    assert row["next_gate"] == "rights_review"


def test_public_discovery_is_explicit_without_claiming_complete_ingestion() -> None:
    page = PAGE.read_text(encoding="utf-8")
    sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
    home = (ROOT / "index.html").read_text(encoding="utf-8")
    catalog = json.loads((ROOT / "config" / "public_data_catalog.json").read_text(encoding="utf-8"))
    assert "Evidence coverage contract" in page
    assert "Publication is not a claim that every registered source has been ingested" in page
    assert "https://palimpsest.info/belt-and-road/" in sitemap
    assert 'href="/belt-and-road/"' in home
    entry = next(item for item in catalog["datasets"] if item["id"] == "belt-and-road-observatory")
    assert entry["status"] == "warming"
    assert entry["latest"] == "readings/belt-and-road-observatory-latest.json"


def test_coverage_contract_is_context_not_independent_evidence() -> None:
    mesh = json.loads((ROOT / "readings" / "evidence-mesh-latest.json").read_text(encoding="utf-8"))
    resource = next(
        item for item in mesh["resources"]
        if item["resource_id"] == "palimpsest:catalog:belt-and-road-observatory"
    )
    assert resource["allowed_role"] == "context"
    assert resource["evidence_class"] == "METHOD_OR_ASSUMPTION"
    assert resource["independence_eligible"] is False
