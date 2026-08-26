"""Strict integration contracts for the BRI observatory WDI v2 descriptor."""

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from collectors.bri_world_bank_wdi import (
    build_url,
    load_registry as load_wdi_registry,
    parse_response,
)
from core.bri_observation import canonical_json_bytes, sha256_bytes
from core.collector_artifact import project_reading
from core.evidence_mesh import build_evidence_mesh
from processors.bri_observatory import (
    BriRegistryError,
    build_wdi_observation_descriptor,
    load_registry,
    validate_observation_dataset_descriptor,
)
from scripts.build_bri_observatory import build
from scripts.build_osint_china import EXCLUDED_LATEST_FILES, SIGNALS


ROOT = Path(__file__).resolve().parents[1]
BRI_REGISTRY = ROOT / "config" / "bri_observatory.json"
WDI_REGISTRY = ROOT / "config" / "bri_wdi_series.json"
WDI_SCHEMA = ROOT / "protocol" / "bri-economic-observations-v1.schema.json"
WDI_REGISTRY_SCHEMA = ROOT / "protocol" / "bri-wdi-series-v1.schema.json"
BRI_V1_SCHEMA = ROOT / "protocol" / "belt-and-road-observatory-v1.schema.json"
BRI_V2_SCHEMA = ROOT / "protocol" / "belt-and-road-observatory-v2.schema.json"
FROZEN_V1 = ROOT / "readings" / "belt-and-road-observatory-v1.json"
RETRIEVED_AT = datetime(2026, 8, 26, 10, 30, tzinfo=UTC)


def _full_wdi_bundle() -> dict:
    registry = load_wdi_registry(WDI_REGISTRY)
    rows = []
    countries = {
        code: {"id": binding.api_country_id, "value": binding.name}
        for code, binding in registry.countries.items()
    }
    for indicator_position, (indicator_id, binding) in enumerate(
        registry.bindings.items()
    ):
        for country_position, country_code in enumerate(sorted(registry.countries)):
            position = indicator_position * len(countries) + country_position
            value: float | None = float(position + 1)
            obs_status = ""
            footnote = ""
            if position == 0:
                obs_status = "F"
                footnote = "Source marked forecast."
            elif position == 1:
                value = None
                footnote = "Source value unavailable for this dataset vintage."
            rows.append(
                {
                    "indicator": {"id": indicator_id, "value": binding.source_title},
                    "country": countries[country_code],
                    "countryiso3code": country_code,
                    "date": "2024",
                    "value": value,
                    "unit": "",
                    "scale": "",
                    "obs_status": obs_status,
                    "decimal": 2,
                    "footnote": footnote,
                }
            )
    response = [
        {
            "page": 1,
            "pages": 1,
            "per_page": 20_000,
            "total": len(rows),
            "sourceid": None,
            "lastupdated": "2026-07-13",
        },
        rows,
    ]
    raw = json.dumps(response, separators=(",", ":")).encode("utf-8")
    collection = parse_response(
        raw,
        registry=registry,
        evidence_url=build_url(registry, start_year=2024, end_year=2024),
        start_year=2024,
        end_year=2024,
        retrieved_at=RETRIEVED_AT,
    )
    return collection.to_dict()


def _bundle_path(tmp_path: Path, document: dict | None = None) -> Path:
    path = tmp_path / "bri-economic-observations-latest.json"
    path.write_bytes(canonical_json_bytes(document or _full_wdi_bundle()))
    return path


def _reseal(document: dict) -> dict:
    payload = deepcopy(document)
    payload.pop("collection_id", None)
    payload["collection_id"] = sha256_bytes(canonical_json_bytes(payload))
    return payload


def _descriptor_context(tmp_path: Path):
    bundle_path = _bundle_path(tmp_path)
    registry = load_registry(BRI_REGISTRY)
    descriptor = build_wdi_observation_descriptor(
        registry,
        bundle_path=bundle_path,
        observation_schema_path=WDI_SCHEMA,
        series_registry_path=WDI_REGISTRY,
    )
    bundle_raw = bundle_path.read_bytes()
    return {
        "registry": registry,
        "descriptor": descriptor,
        "artifact_raw": bundle_raw,
        "artifact_document": json.loads(bundle_raw),
        "observation_schema_raw": WDI_SCHEMA.read_bytes(),
        "series_registry_raw": WDI_REGISTRY.read_bytes(),
        "series_registry_path": WDI_REGISTRY,
    }


def test_v1_contract_and_reading_are_frozen_byte_for_byte() -> None:
    assert hashlib.sha256(BRI_V1_SCHEMA.read_bytes()).hexdigest() == (
        "6b68e84712bf126c581377c2203b4a40d055ae2b665c8ceb4246e273da4376ec"
    )
    frozen = FROZEN_V1.read_bytes()
    assert len(frozen) == 89_584
    assert hashlib.sha256(frozen).hexdigest() == (
        "4716ccedb6e567f0c18f9d2467e5a6fd496cad8f3e035ce47f0918698e6a690e"
    )
    document = json.loads(frozen)
    schema = json.loads(BRI_V1_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    assert document["schema_version"] == "palimpsest.belt-and-road-observatory.v1"


def test_v2_binds_exact_bundle_contracts_counts_clocks_rights_and_boundaries(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle_path(tmp_path)
    json_bytes, html_bytes = build(
        BRI_REGISTRY,
        wdi_bundle_path=bundle_path,
    )
    artifact = json.loads(json_bytes)
    schema = json.loads(BRI_V2_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(artifact)

    assert artifact["schema_version"] == "palimpsest.belt-and-road-observatory.v2"
    [descriptor] = artifact["observation_datasets"]
    expected_implementation = next(
        source["implementation"]
        for source in load_registry(BRI_REGISTRY)["sources"]
        if source["source_id"] == "world_bank_wdi"
    )
    assert descriptor["implementation_state"] == expected_implementation
    assert descriptor["publication_state"] == "repository_ready_not_deployed"
    assert descriptor["publication_receipt"] is None
    assert descriptor["artifact"] == {
        "path": "readings/bri-economic-observations-latest.json",
        "url": "https://palimpsest.info/readings/bri-economic-observations-latest.json",
        "media_type": "application/json",
        "bytes": len(bundle_path.read_bytes()),
        "sha256": sha256_bytes(bundle_path.read_bytes()),
    }
    assert descriptor["observation_schema"]["sha256"] == sha256_bytes(
        WDI_SCHEMA.read_bytes()
    )
    assert descriptor["series_registry"]["sha256"] == sha256_bytes(
        WDI_REGISTRY.read_bytes()
    )
    assert descriptor["coverage"] == {
        "start_year": 2024,
        "end_year": 2024,
        "countries": 3,
        "indicators": 18,
        "source_rows": 54,
        "observed_rows": 52,
        "forecast_rows": 1,
        "unavailable_rows": 1,
    }
    assert descriptor["clocks"] == {
        "dataset_last_updated": "2026-07-13",
        "source_release_upper_bound": "2026-07-13T23:59:59Z",
        "retrieved_at": "2026-08-26T10:30:00Z",
    }
    assert descriptor["context_boundary"]["allowed_role"] == "context"
    assert all(
        descriptor["context_boundary"][field] == "prohibited"
        for field in (
            "project_inference",
            "actor_inference",
            "corridor_inference",
            "causal_inference",
            "tactical_data",
        )
    )
    page = html_bytes.decode("utf-8")
    assert "Country-period context" in page
    assert "Source-marked forecasts" not in page
    assert "repository ready not deployed" in page


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda bundle: bundle.update(collection_id="0" * 64),
            "collection_id",
        ),
        (
            lambda bundle: (
                bundle["coverage"].update(observed_rows=51),
                bundle["request_receipts"][0].update(observed_rows=51),
            ),
            "evidence-state counts",
        ),
        (
            lambda bundle: bundle.update(generated_at="2026-08-26T10:31:00Z"),
            "retrieval clock",
        ),
    ],
)
def test_bundle_identity_count_and_clock_mismatches_fail_closed(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    bundle = _full_wdi_bundle()
    mutation(bundle)
    if message != "collection_id":
        bundle = _reseal(bundle)
    path = _bundle_path(tmp_path, bundle)
    with pytest.raises(BriRegistryError, match=message):
        build_wdi_observation_descriptor(
            load_registry(BRI_REGISTRY),
            bundle_path=path,
            observation_schema_path=WDI_SCHEMA,
            series_registry_path=WDI_REGISTRY,
        )


def test_registry_clock_descriptor_state_hash_and_boundary_mismatches_fail_closed(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle_path(tmp_path)
    repository_ready_registry = deepcopy(load_registry(BRI_REGISTRY))
    next(
        source
        for source in repository_ready_registry["sources"]
        if source["source_id"] == "world_bank_wdi"
    )["implementation"] = "repository_ready"
    repository_ready = build_wdi_observation_descriptor(
        repository_ready_registry,
        bundle_path=bundle_path,
        observation_schema_path=WDI_SCHEMA,
        series_registry_path=WDI_REGISTRY,
    )
    assert repository_ready["implementation_state"] == "repository_ready"
    assert repository_ready["publication_state"] == "repository_ready_not_deployed"
    assert repository_ready["publication_receipt"] is None

    stale_registry = deepcopy(load_registry(BRI_REGISTRY))
    stale_registry["as_of"] = "2026-08-26T10:29:59Z"
    with pytest.raises(BriRegistryError, match="as_of precedes"):
        build_wdi_observation_descriptor(
            stale_registry,
            bundle_path=bundle_path,
            observation_schema_path=WDI_SCHEMA,
            series_registry_path=WDI_REGISTRY,
        )

    context = _descriptor_context(tmp_path)
    mutations = (
        (lambda row: row["artifact"].update(sha256="0" * 64), "hash mismatch"),
        (
            lambda row: row.update(
                implementation_state=(
                    "repository_ready"
                    if row["implementation_state"] == "adapter_ready"
                    else "adapter_ready"
                )
            ),
            "state mismatches",
        ),
        (
            lambda row: row["context_boundary"].update(project_inference="allowed"),
            "boundary was weakened",
        ),
        (
            lambda row: row["coverage"].update(observed_rows=51, forecast_rows=2),
            "coverage counts mismatch",
        ),
        (
            lambda row: row["clocks"].update(
                dataset_last_updated="2026-07-14",
                source_release_upper_bound="2026-07-14T23:59:59Z",
            ),
            "clocks mismatch",
        ),
        (
            lambda row: row.update(publication_receipt={"status": "success"}),
            "must be null",
        ),
    )
    for mutate, message in mutations:
        candidate = deepcopy(context["descriptor"])
        mutate(candidate)
        with pytest.raises(BriRegistryError, match=message):
            validate_observation_dataset_descriptor(
                candidate,
                **{key: value for key, value in context.items() if key != "descriptor"},
            )


def test_collector_projection_keeps_one_request_receipt_and_separate_states(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle_path(tmp_path)
    artifact = project_reading(bundle_path, collector_id="bri-world-bank-wdi")
    assert artifact["collector_id"] == "bri-world-bank-wdi"
    assert artifact["payload_sha256"] == sha256_bytes(bundle_path.read_bytes())
    assert artifact["source_receipt"]["retrieved_at"] == "2026-08-26T10:30:00Z"
    assert artifact["source_receipt"]["counts"] == {
        "source_rows": 54,
        "observed_rows": 52,
        "forecast_rows": 1,
        "unavailable_rows": 1,
    }
    assert artifact["coverage"]["observed_rows"] == 52
    assert artifact["coverage"]["forecast_rows"] == 1
    assert artifact["freshness"] == {
        "evidence_state": "fresh",
        "observed_at": "2026-07-13T23:59:59Z",
        "knowledge_time": "2026-08-26T10:30:00Z",
        "generated_at": "2026-08-26T10:30:00Z",
        "release_time_semantics": "dataset_lastupdated_upper_bound",
    }


def _copy_mesh_inputs(target: Path) -> None:
    config = json.loads((ROOT / "config" / "evidence_mesh.json").read_text())
    paths = ["config/evidence_mesh.json"]
    paths.extend(
        contract["local_path"]
        for project in config["projects"]
        for contract in project["input_contracts"]
        if contract["local_path"] is not None
    )
    paths.extend(
        [
            "config/bri_wdi_series.json",
            "protocol/bri-economic-observations-v1.schema.json",
        ]
    )
    for relative in sorted(set(paths)):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)


def test_evidence_mesh_projects_wdi_as_nonindependent_context_with_null_publication_clock(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle_path(tmp_path)
    v2_json, _v2_html = build(BRI_REGISTRY, wdi_bundle_path=bundle_path)
    mesh_root = tmp_path / "mesh"
    _copy_mesh_inputs(mesh_root)
    reading = mesh_root / "readings" / "belt-and-road-observatory-latest.json"
    reading.parent.mkdir(parents=True, exist_ok=True)
    reading.write_bytes(v2_json)
    wdi_artifact = mesh_root / "readings" / "bri-economic-observations-latest.json"
    wdi_artifact.write_bytes(bundle_path.read_bytes())

    mesh = build_evidence_mesh(
        mesh_root,
        now=datetime(2026, 8, 26, 14, 0, 0, tzinfo=UTC),
    )
    resource = next(
        row
        for row in mesh["resources"]
        if row["resource_id"] == "palimpsest:context:bri-world-bank-wdi"
    )
    assert resource["allowed_role"] == "context"
    assert resource["independence_eligible"] is False
    assert resource["rights"] == {
        "redistribution": "ATTRIBUTION_REQUIRED",
        "reuse": "full_text",
        "training": "prohibited",
    }
    assert resource["clocks"] == {
        "event_time": None,
        "knowledge_time": "2026-08-26T10:30:00Z",
        "publication_time": None,
    }
    assert resource["source_temporal_coverage"] == {
        "kind": "year_range",
        "from_year": 2024,
        "to_year": 2024,
        "snapshot_date": None,
    }
    receipt = next(
        row
        for row in mesh["inputs"]
        if row["input_id"] == "palimpsest-bri-wdi-world-bank"
    )
    assert receipt["byte_identity"] == "match"
    assert receipt["resource_count"] == 54
    assert receipt["sha256"] == sha256_bytes(bundle_path.read_bytes())


def test_wdi_registry_schema_and_osint_exclusion_are_explicit() -> None:
    schema = json.loads(WDI_REGISTRY_SCHEMA.read_text(encoding="utf-8"))
    registry = json.loads(WDI_REGISTRY.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(registry)
    assert "bri-economic-observations-latest.json" in EXCLUDED_LATEST_FILES
    assert not any(
        signal.filename == "bri-economic-observations-latest.json"
        for signal in SIGNALS
    )
