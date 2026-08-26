"""Fail-closed contracts for the production-verified NarcoScope v2 pin."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.narcoscope_corridor_bridge import (
    ARTIFACT_ID,
    CANONICAL_URL,
    DEFAULT_ARTIFACT_PATH,
    DEFAULT_SCHEMA_PATH,
    SCHEMA_VERSION,
    NarcoScopeCorridorError,
    admission_receipt,
    load_bundle,
    validate_artifact,
    validate_receipt,
)
from scripts import sync_narcoscope_corridors


ROOT = Path(__file__).resolve().parents[1]


def _bundle() -> tuple[dict, dict, dict, bytes, bytes]:
    artifact, schema, receipt = load_bundle()
    return (
        artifact,
        schema,
        receipt,
        DEFAULT_ARTIFACT_PATH.read_bytes(),
        DEFAULT_SCHEMA_PATH.read_bytes(),
    )


def test_checked_in_v2_bundle_is_exactly_pinned_and_production_verified() -> None:
    artifact, schema, receipt, artifact_raw, schema_raw = _bundle()
    assert artifact["schemaVersion"] == SCHEMA_VERSION
    assert artifact["artifactId"] == ARTIFACT_ID
    assert schema["$id"].endswith("/narcoscope-palimpsest-corridors-v2.schema.json")
    assert receipt["source_url"] == CANONICAL_URL
    assert receipt["status"] == "production_verified"
    assert receipt["deployment"]["commit_sha"] == "5bf6a31cfd98e56dadca495f35b99ecb73c1d74f"
    assert receipt["deployment"]["deployment_id"] == 6103284752
    assert receipt["deployment"]["test_run_id"] == 32966260157
    assert receipt["deployment"]["registry_run_id"] == 32966416333
    assert receipt["current"]["sha256"] == hashlib.sha256(artifact_raw).hexdigest()
    assert receipt["current"]["schema_sha256"] == hashlib.sha256(schema_raw).hexdigest()
    assert sync_narcoscope_corridors.main(["--check"]) == 0


def test_v2_country_coverage_and_missing_values_are_preserved() -> None:
    artifact, _, _, _, _ = _bundle()
    assert [item["iso3"] for item in artifact["geographies"]] == ["CHN", "MMR", "PAK"]
    prices = artifact["datasets"]["retailDrugPrices"]["data"]["countries"]
    assert [(row["geography"]["iso3"], row["recordCount"]) for row in prices] == [
        ("CHN", 4), ("MMR", 3), ("PAK", 2),
    ]
    wildlife = artifact["datasets"]["wildlifeConfiscations"]["data"]["countries"]
    myanmar = next(row for row in wildlife if row["geography"]["iso3"] == "MMR")
    assert myanmar["exporterOfRecord"] == {
        "coverageStatus": "not_in_retained_top_table",
        "recordCount": None,
        "rankInRetainedTable": None,
    }


def test_v2_refuses_a_fabricated_china_myanmar_precursor_link() -> None:
    artifact, schema, _, _, _ = _bundle()
    candidate = json.loads(json.dumps(artifact))
    record = next(
        row for row in candidate["datasets"]["precursorCorridorIncidents"]["data"]["corridors"]
        if row["destination"] == "Myanmar"
    )
    record["reportedOrigin"] = "China"
    record["geographyMatches"] = ["CHN", "MMR"]
    candidate["datasets"]["precursorCorridorIncidents"]["data"]["crossTargetBilateralRecordCount"] = 1
    with pytest.raises(NarcoScopeCorridorError, match="bilateral|schema"):
        validate_artifact(candidate, schema)


def test_v2_refuses_missing_as_zero_actor_inference_and_tactical_fields() -> None:
    artifact, schema, _, _, _ = _bundle()

    missing_as_zero = json.loads(json.dumps(artifact))
    role = missing_as_zero["datasets"]["wildlifeConfiscations"]["data"]["countries"][1]["exporterOfRecord"]
    role["recordCount"] = 0
    with pytest.raises(NarcoScopeCorridorError, match="schema|numeric zero"):
        validate_artifact(missing_as_zero, schema)

    actor_inference = json.loads(json.dumps(artifact))
    actor_inference["disclosure"]["politicalOrArmedActorInference"] = "allowed"
    with pytest.raises(NarcoScopeCorridorError, match="schema|disclosure"):
        validate_artifact(actor_inference, schema)

    tactical = json.loads(json.dumps(artifact))
    tactical["datasets"]["drugSeizures"]["data"]["latitude"] = 25.0
    with pytest.raises(NarcoScopeCorridorError, match="schema|forbidden"):
        validate_artifact(tactical, schema)


def test_receipt_cannot_bless_changed_artifact_or_schema_bytes() -> None:
    artifact, _, receipt, artifact_raw, schema_raw = _bundle()
    with pytest.raises(NarcoScopeCorridorError, match="artifact bytes"):
        validate_receipt(
            receipt,
            artifact_raw=artifact_raw + b" ",
            schema_raw=schema_raw,
            artifact=artifact,
        )
    with pytest.raises(NarcoScopeCorridorError, match="schema bytes"):
        validate_receipt(
            receipt,
            artifact_raw=artifact_raw,
            schema_raw=schema_raw + b" ",
            artifact=artifact,
        )


def test_production_proof_is_structured_and_new_admission_resets_it() -> None:
    artifact, _, receipt, artifact_raw, schema_raw = _bundle()
    changed = json.loads(json.dumps(receipt))
    changed["deployment"]["verification_checks"] = ["github_deployment_success"]
    with pytest.raises(NarcoScopeCorridorError, match="verification checks"):
        validate_receipt(
            changed,
            artifact_raw=artifact_raw,
            schema_raw=schema_raw,
            artifact=artifact,
        )

    next_receipt = admission_receipt(
        artifact,
        artifact_raw,
        schema_raw,
        admitted_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        previous=receipt,
    )
    assert next_receipt["status"] == "repository_ready_not_deployed"
    assert "deployment" not in next_receipt
    assert validate_receipt(
        next_receipt,
        artifact_raw=artifact_raw,
        schema_raw=schema_raw,
        artifact=artifact,
    ) == next_receipt


def test_bri_contract_points_to_the_pinned_local_v2_path() -> None:
    registry = json.loads((ROOT / "config" / "bri_observatory.json").read_text(encoding="utf-8"))
    [bridge] = registry["partner_bridges"]
    assert ROOT / bridge["palimpsest_path"] == DEFAULT_ARTIFACT_PATH
    assert bridge["status"] == "production_verified"
    assert bridge["join_policy"] == "geography_and_time_only"
