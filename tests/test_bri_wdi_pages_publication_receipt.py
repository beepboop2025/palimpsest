"""Fail-closed tests for the BRI WDI Pages production-proof transition."""

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core.bri_observation import canonical_json_bytes, sha256_bytes
from core.evidence_mesh import EvidenceMeshError, build_evidence_mesh
from core.pages_publication_receipt import (
    PAGES_PUBLICATION_SCHEMA_VERSION,
    PagesPublicationReceiptError,
    build_pages_publication_locator,
    load_pages_publication_receipt,
)
from processors.bri_observatory import (
    BriRegistryError,
    build_wdi_observation_descriptor,
    load_registry,
    validate_observation_dataset_descriptor_shape,
)
from scripts import build_bri_observatory as bri_builder
from scripts.build_bri_observatory import build


ROOT = Path(__file__).resolve().parents[1]
BRI_REGISTRY = ROOT / "config" / "bri_observatory.json"
WDI_BUNDLE = ROOT / "readings" / "bri-economic-observations-latest.json"
WDI_SCHEMA = ROOT / "protocol" / "bri-economic-observations-v1.schema.json"
WDI_SERIES_REGISTRY = ROOT / "config" / "bri_wdi_series.json"
BRI_V2_SCHEMA = ROOT / "protocol" / "belt-and-road-observatory-v2.schema.json"
RECEIPT_SCHEMA = ROOT / "protocol" / "bri-wdi-pages-publication-v1.schema.json"
FROZEN_V1 = ROOT / "readings" / "belt-and-road-observatory-v1.json"
PUBLICATION_SHA = "a" * 40
SIZE_RECEIPT_PATH = f".well-known/receipts/pages-artifact-size-{PUBLICATION_SHA}.json"
RELEASE_A_SHA = "14b06772dfed6cdc736279c9ab61b444e5846598"
CHECKED_RECEIPT = (
    ROOT / ".well-known" / "receipts" / "bri-wdi-pages-publication-v1.json"
)
CHECKED_SIZE_RECEIPT = (
    ROOT
    / ".well-known"
    / "receipts"
    / f"pages-artifact-size-{RELEASE_A_SHA}.json"
)
CHECKED_RECEIPT_SHA256 = (
    "239a6b5e1496eaf3f97d8d0502cbf1581f24b02ba386d7d806adc79a877d2a06"
)
CHECKED_VERIFIED_AT = "2026-08-26T15:55:34Z"
CHECKED_FRESH_UNTIL = "2026-08-27T15:55:34Z"


def _resource(path: str) -> dict:
    raw = (ROOT / path).read_bytes()
    digest = sha256_bytes(raw)
    return {
        "path": path,
        "url": f"https://palimpsest.info/{path}?sha256={digest}",
        "http_status": 200,
        "bytes": len(raw),
        "sha256": digest,
    }


def _expected_resources() -> dict[str, dict]:
    return {
        resource["path"]: {
            "bytes": resource["bytes"],
            "sha256": resource["sha256"],
        }
        for resource in (
            _resource("readings/bri-economic-observations-latest.json"),
            _resource("protocol/bri-economic-observations-v1.schema.json"),
            _resource("config/bri_wdi_series.json"),
        )
    }


def _size_receipt() -> dict:
    artifact_bytes = 920_000_000
    limit_bytes = 1_048_576_000
    return {
        "artifact_bytes": artifact_bytes,
        "artifact_name": "github-pages/artifact.tar",
        "artifact_sha256": "b" * 64,
        "headroom_bytes": limit_bytes - artifact_bytes,
        "limit_bytes": limit_bytes,
        "publication_sha": PUBLICATION_SHA,
        "schema_version": "palimpsest.pages-artifact-size.v1",
        "status": "within-limit",
    }


def _receipt(size_document: dict, size_raw: bytes) -> dict:
    collection_id = json.loads(WDI_BUNDLE.read_bytes())["collection_id"]
    resources = [
        _resource("readings/bri-economic-observations-latest.json"),
        _resource("protocol/bri-economic-observations-v1.schema.json"),
        _resource("config/bri_wdi_series.json"),
    ]
    return {
        "schema_version": PAGES_PUBLICATION_SCHEMA_VERSION,
        "status": "production_verified",
        "dataset_id": "bri-economic-context-world-bank-wdi",
        "source_id": "world_bank_wdi",
        "collection_id": collection_id,
        "workflow": {
            "repository": "beepboop2025/palimpsest",
            "publication_sha": PUBLICATION_SHA,
            "workflow_path": ".github/workflows/tests.yml",
            "run_id": 329_743_987_40,
            "run_attempt": 1,
            "run_url": (
                "https://github.com/beepboop2025/palimpsest/actions/runs/32974398740"
            ),
            "run_api_url": (
                "https://api.github.com/repos/beepboop2025/palimpsest/"
                "actions/runs/32974398740"
            ),
            "event": "repository_dispatch",
            "branch": "main",
            "conclusion": "success",
            "pages_package_job_id": 98_206_101_695,
            "pages_deploy_job_id": 98_206_101_696,
            "pages_package_job": {
                "id": 98_206_101_695,
                "name": "Package exact complete Pages edition",
                "api_url": (
                    "https://api.github.com/repos/beepboop2025/palimpsest/"
                    "actions/jobs/98206101695"
                ),
                "html_url": (
                    "https://github.com/beepboop2025/palimpsest/actions/runs/"
                    "32974398740/job/98206101695"
                ),
                "run_id": 329_743_987_40,
                "run_attempt": 1,
                "head_sha": PUBLICATION_SHA,
                "conclusion": "success",
            },
            "pages_deploy_job": {
                "id": 98_206_101_696,
                "name": "Deploy exact complete Pages edition",
                "api_url": (
                    "https://api.github.com/repos/beepboop2025/palimpsest/"
                    "actions/jobs/98206101696"
                ),
                "html_url": (
                    "https://github.com/beepboop2025/palimpsest/actions/runs/"
                    "32974398740/job/98206101696"
                ),
                "run_id": 329_743_987_40,
                "run_attempt": 1,
                "head_sha": PUBLICATION_SHA,
                "conclusion": "success",
            },
        },
        "pages_artifact": {
            "id": 9_610_112_618,
            "name": "github-pages",
            "api_url": (
                "https://api.github.com/repos/beepboop2025/palimpsest/"
                "actions/artifacts/9610112618"
            ),
            "archive_bytes": 143_750_471,
            "digest_sha256": "c" * 64,
            "workflow_run_id": 329_743_987_40,
            "workflow_run_head_sha": PUBLICATION_SHA,
            "created_at": "2026-08-26T14:00:52Z",
            "expires_at": "2026-08-27T14:00:52Z",
            "captured_at": "2026-08-26T14:02:00Z",
        },
        "archived_size_receipt": {
            "artifact_id": 9_610_113_838,
            "artifact_name": f"pages-artifact-size-{PUBLICATION_SHA}",
            "artifact_api_url": (
                "https://api.github.com/repos/beepboop2025/palimpsest/"
                "actions/artifacts/9610113838"
            ),
            "archive_bytes": 392,
            "digest_sha256": "d" * 64,
            "workflow_run_id": 329_743_987_40,
            "workflow_run_head_sha": PUBLICATION_SHA,
            "checked_in_path": SIZE_RECEIPT_PATH,
            "public_url": f"https://palimpsest.info/{SIZE_RECEIPT_PATH}",
            "bytes": len(size_raw),
            "sha256": sha256_bytes(size_raw),
            "parsed": size_document,
        },
        "deployment": {
            "deployment_id": 8_100_001,
            "deployment_api_url": (
                "https://api.github.com/repos/beepboop2025/palimpsest/"
                "deployments/8100001"
            ),
            "sha": PUBLICATION_SHA,
            "ref": "main",
            "environment": "github-pages",
            "environment_url": "https://palimpsest.info/",
            "success_status_id": 8_100_002,
            "success_status_api_url": (
                "https://api.github.com/repos/beepboop2025/palimpsest/"
                "deployments/8100001/statuses/8100002"
            ),
            "success_status_deployment_url": (
                "https://api.github.com/repos/beepboop2025/palimpsest/"
                "deployments/8100001"
            ),
            "state_at_verification": "success",
            "deployed_at": "2026-08-26T14:03:00Z",
            "log_url": (
                "https://github.com/beepboop2025/palimpsest/actions/runs/"
                "32974398740/job/98206101696"
            ),
        },
        "served_verification": {
            "verified_at": "2026-08-26T14:05:00Z",
            "method": "cache_busted_https_get",
            "resources": resources,
        },
    }


def _write_evidence(
    tmp_path: Path,
    *,
    receipt_mutation=None,
    size_mutation=None,
    canonical_size: bool = True,
) -> tuple[Path, Path, dict, bytes]:
    size_document = _size_receipt()
    if size_mutation is not None:
        size_mutation(size_document)
    if canonical_size:
        size_raw = canonical_json_bytes(size_document)
    else:
        size_raw = (json.dumps(size_document, indent=2) + "\n").encode("utf-8")
    receipt = _receipt(size_document, size_raw)
    if receipt_mutation is not None:
        receipt_mutation(receipt)
    receipt_raw = canonical_json_bytes(receipt)
    receipt_path = tmp_path / "bri-wdi-pages-publication-v1.json"
    size_path = tmp_path / f"pages-artifact-size-{PUBLICATION_SHA}.json"
    receipt_path.write_bytes(receipt_raw)
    size_path.write_bytes(size_raw)
    return receipt_path, size_path, receipt, receipt_raw


def _live_registry() -> dict:
    registry = deepcopy(load_registry(BRI_REGISTRY))
    registry["as_of"] = "2026-08-26T14:10:00Z"
    next(
        source
        for source in registry["sources"]
        if source["source_id"] == "world_bank_wdi"
    )["implementation"] = "live"
    return registry


def _repository_ready_registry() -> dict:
    registry = deepcopy(load_registry(BRI_REGISTRY))
    next(
        source
        for source in registry["sources"]
        if source["source_id"] == "world_bank_wdi"
    )["implementation"] = "repository_ready"
    return registry


def _repository_ready_registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "repository-ready-bri-registry.json"
    path.write_text(
        json.dumps(_repository_ready_registry(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _copy_evidence_mesh_inputs(target: Path) -> None:
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


def _build_descriptor(receipt_path: Path, size_path: Path) -> dict:
    return build_wdi_observation_descriptor(
        _live_registry(),
        bundle_path=WDI_BUNDLE,
        observation_schema_path=WDI_SCHEMA,
        series_registry_path=WDI_SERIES_REGISTRY,
        publication_receipt_path=receipt_path,
        archived_size_receipt_path=size_path,
    )


def test_fixture_receipt_is_canonical_schema_valid_and_exactly_bound(
    tmp_path: Path,
) -> None:
    receipt_path, size_path, receipt, receipt_raw = _write_evidence(tmp_path)
    receipt_schema = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(receipt_schema)
    Draft202012Validator(
        receipt_schema,
        format_checker=FormatChecker(),
    ).validate(receipt)

    loaded = load_pages_publication_receipt(
        receipt_path,
        archived_size_receipt_path=size_path,
        expected_dataset_id="bri-economic-context-world-bank-wdi",
        expected_source_id="world_bank_wdi",
        expected_collection_id=receipt["collection_id"],
        expected_resources=_expected_resources(),
        verification_cutoff=datetime(2026, 8, 26, 14, 10, tzinfo=UTC),
    )
    assert loaded.document == receipt
    assert loaded.raw == receipt_raw == canonical_json_bytes(receipt)
    assert loaded.archived_size_receipt_raw == canonical_json_bytes(_size_receipt())
    with pytest.raises(TypeError, match="validated receipt result"):
        build_pages_publication_locator(
            receipt,
            repository_path=(".well-known/receipts/bri-wdi-pages-publication-v1.json"),
        )


def test_checked_in_release_a_receipt_promotes_the_exact_current_bundle() -> None:
    receipt_raw = CHECKED_RECEIPT.read_bytes()
    size_raw = CHECKED_SIZE_RECEIPT.read_bytes()
    assert len(receipt_raw) == 4_846
    assert sha256_bytes(receipt_raw) == CHECKED_RECEIPT_SHA256
    assert len(size_raw) == 348
    assert sha256_bytes(size_raw) == (
        "16b096ee5be62da4eaa24331b5340bd8bbdc74186f3990dbbabae040d446af5b"
    )

    receipt = json.loads(receipt_raw)
    bundle = json.loads(WDI_BUNDLE.read_bytes())
    validated = load_pages_publication_receipt(
        CHECKED_RECEIPT,
        archived_size_receipt_path=CHECKED_SIZE_RECEIPT,
        expected_dataset_id="bri-economic-context-world-bank-wdi",
        expected_source_id="world_bank_wdi",
        expected_collection_id=bundle["collection_id"],
        expected_resources=_expected_resources(),
        verification_cutoff=datetime(2026, 8, 26, 15, 55, 34, tzinfo=UTC),
    )
    assert validated.raw == receipt_raw == canonical_json_bytes(receipt)
    assert receipt["workflow"]["publication_sha"] == RELEASE_A_SHA
    assert receipt["workflow"]["run_id"] == 32_984_946_320
    assert receipt["served_verification"]["verified_at"] == CHECKED_VERIFIED_AT

    descriptor = build_wdi_observation_descriptor(
        load_registry(BRI_REGISTRY),
        bundle_path=WDI_BUNDLE,
        observation_schema_path=WDI_SCHEMA,
        series_registry_path=WDI_SERIES_REGISTRY,
        publication_receipt_path=CHECKED_RECEIPT,
        archived_size_receipt_path=CHECKED_SIZE_RECEIPT,
    )
    assert descriptor["implementation_state"] == "live"
    assert descriptor["publication_state"] == "production_verified"
    assert descriptor["publication_receipt"] == {
        "schema_version": "palimpsest.bri-wdi-pages-publication-locator.v1",
        "status": "production_verified",
        "repository_path": (
            ".well-known/receipts/bri-wdi-pages-publication-v1.json"
        ),
        "public_url": (
            "https://palimpsest.info/.well-known/receipts/"
            "bri-wdi-pages-publication-v1.json"
        ),
        "receipt_sha256": CHECKED_RECEIPT_SHA256,
        "release_a_sha": RELEASE_A_SHA,
        "verified_at": CHECKED_VERIFIED_AT,
        "fresh_until": CHECKED_FRESH_UNTIL,
        "availability_semantics": (
            "verified_at_release_not_continuous_monitoring"
        ),
    }


def test_validated_receipt_mapping_mutation_cannot_change_locator(
    tmp_path: Path,
) -> None:
    receipt_path, size_path, receipt, _ = _write_evidence(tmp_path)
    loaded = load_pages_publication_receipt(
        receipt_path,
        archived_size_receipt_path=size_path,
        expected_dataset_id="bri-economic-context-world-bank-wdi",
        expected_source_id="world_bank_wdi",
        expected_collection_id=receipt["collection_id"],
        expected_resources=_expected_resources(),
        verification_cutoff=datetime(2026, 8, 26, 14, 10, tzinfo=UTC),
    )
    loaded.document["workflow"]["publication_sha"] = "f" * 40

    locator = build_pages_publication_locator(
        loaded,
        repository_path=".well-known/receipts/bri-wdi-pages-publication-v1.json",
    )
    assert locator["release_a_sha"] == PUBLICATION_SHA
    assert loaded.document["workflow"]["publication_sha"] == PUBLICATION_SHA


def test_production_descriptor_requires_receipt_and_binds_current_exact_resources(
    tmp_path: Path,
) -> None:
    receipt_path, size_path, receipt, receipt_raw = _write_evidence(tmp_path)
    descriptor = _build_descriptor(receipt_path, size_path)
    assert descriptor["publication_state"] == "production_verified"
    assert descriptor["publication_receipt"] == {
        "schema_version": "palimpsest.bri-wdi-pages-publication-locator.v1",
        "status": "production_verified",
        "repository_path": (".well-known/receipts/bri-wdi-pages-publication-v1.json"),
        "public_url": (
            "https://palimpsest.info/.well-known/receipts/"
            "bri-wdi-pages-publication-v1.json"
        ),
        "receipt_sha256": sha256_bytes(receipt_raw),
        "release_a_sha": PUBLICATION_SHA,
        "verified_at": receipt["served_verification"]["verified_at"],
        "fresh_until": "2026-08-27T14:05:00Z",
        "availability_semantics": ("verified_at_release_not_continuous_monitoring"),
    }
    assert descriptor["artifact"]["sha256"] == (
        "68b9f96e3cfc1e5692b4305c93b42b64dd45d065655ccade6947e086285dc099"
    )
    assert descriptor["observation_schema"]["sha256"] == (
        "e0e98cf313a8446667a0749ac49b5160d8e8d41f59eb76c8aaab66fd27e3ea8f"
    )
    assert descriptor["series_registry"]["sha256"] == (
        "5c0c8e9487aa0145ac2cb8f77697d1ac75f75f38eb3f87239fd8c9685104bda9"
    )

    live_registry_path = tmp_path / "live-bri-registry.json"
    live_registry_path.write_text(
        json.dumps(_live_registry(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    json_bytes, html_bytes = build(
        live_registry_path,
        wdi_bundle_path=WDI_BUNDLE,
        wdi_publication_receipt_path=receipt_path,
        wdi_archived_size_receipt_path=size_path,
    )
    artifact = json.loads(json_bytes)
    schema = json.loads(BRI_V2_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(artifact)
    assert artifact["observation_datasets"] == [descriptor]
    page = html_bytes.decode("utf-8")
    assert "production verified" in page
    assert receipt["served_verification"]["verified_at"] in page
    assert "2026-08-27T14:05:00Z" in page
    assert "Inspect the immutable receipt" in page
    assert "release-time proof, not continuous monitoring" in page
    assert "verified_at_release_not_continuous_monitoring" in page


def test_evidence_mesh_loads_production_receipts_offline_and_expires_availability(
    tmp_path: Path,
) -> None:
    receipt_path, size_path, _, _ = _write_evidence(tmp_path)
    live_registry_path = tmp_path / "live-bri-registry.json"
    live_registry_path.write_text(
        json.dumps(_live_registry(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    v2_json, _ = build(
        live_registry_path,
        wdi_bundle_path=WDI_BUNDLE,
        wdi_publication_receipt_path=receipt_path,
        wdi_archived_size_receipt_path=size_path,
    )

    mesh_root = tmp_path / "mesh"
    _copy_evidence_mesh_inputs(mesh_root)
    observatory_path = mesh_root / "readings" / "belt-and-road-observatory-latest.json"
    observatory_path.parent.mkdir(parents=True, exist_ok=True)
    observatory_path.write_bytes(v2_json)
    wdi_path = mesh_root / "readings" / "bri-economic-observations-latest.json"
    wdi_path.write_bytes(WDI_BUNDLE.read_bytes())
    checked_receipt = (
        mesh_root / ".well-known" / "receipts" / "bri-wdi-pages-publication-v1.json"
    )
    checked_receipt.parent.mkdir(parents=True, exist_ok=True)
    checked_receipt.write_bytes(receipt_path.read_bytes())
    checked_size = mesh_root / SIZE_RECEIPT_PATH
    checked_size.write_bytes(size_path.read_bytes())

    mesh = build_evidence_mesh(
        mesh_root,
        now=datetime(2026, 8, 26, 14, 10, tzinfo=UTC),
    )
    resource = next(
        row
        for row in mesh["resources"]
        if row["resource_id"] == "palimpsest:context:bri-world-bank-wdi"
    )
    assert resource["availability"] == "available"
    assert resource["clocks"]["publication_time"] == "2026-08-26T14:05:00Z"
    assert resource["freshness"] == {
        "status": "fresh",
        "observed_at": "2026-08-26T14:05:00Z",
        "deadline": "2026-08-27T14:05:00Z",
        "age_hours": 0.083,
        "cadence": None,
    }
    assert any(
        "point-in-time deployment evidence" in limitation
        for limitation in resource["limitations"]
    )
    assert any(
        "WDI data currency is separate" in limitation
        for limitation in resource["limitations"]
    )
    catalog_resource = next(
        row
        for row in mesh["resources"]
        if row["resource_id"]
        == "palimpsest:catalog:bri-economic-observations"
    )
    assert catalog_resource["public_url"] == resource["public_url"]
    assert catalog_resource["allowed_role"] == "context"
    assert catalog_resource["independence_eligible"] is False
    assert catalog_resource["availability"] == resource["availability"]
    assert catalog_resource["clocks"] == resource["clocks"]
    assert catalog_resource["freshness"] == resource["freshness"]
    assert catalog_resource["dependency_resource_ids"] == [
        "palimpsest:context:bri-world-bank-wdi"
    ]
    input_receipt = next(
        row
        for row in mesh["inputs"]
        if row["input_id"] == "palimpsest-bri-wdi-world-bank"
    )
    assert input_receipt["availability"] == "available"
    assert input_receipt["observed_at"] == "2026-08-26T14:05:00Z"
    assert "production-verified by the immutable receipt" in input_receipt["reason"]

    stale_mesh = build_evidence_mesh(
        mesh_root,
        now=datetime(2026, 8, 27, 14, 5, 1, tzinfo=UTC),
    )
    stale_resource = next(
        row
        for row in stale_mesh["resources"]
        if row["resource_id"] == "palimpsest:context:bri-world-bank-wdi"
    )
    assert stale_resource["availability"] == "stale"
    assert stale_resource["freshness"]["status"] == "stale"
    stale_catalog_resource = next(
        row
        for row in stale_mesh["resources"]
        if row["resource_id"]
        == "palimpsest:catalog:bri-economic-observations"
    )
    assert stale_catalog_resource["availability"] == "stale"
    assert stale_catalog_resource["clocks"] == stale_resource["clocks"]
    assert stale_catalog_resource["freshness"] == stale_resource["freshness"]
    stale_input = next(
        row
        for row in stale_mesh["inputs"]
        if row["input_id"] == "palimpsest-bri-wdi-world-bank"
    )
    assert stale_input["availability"] == "stale"
    assert (
        "current availability requires a new public re-probe" in stale_input["reason"]
    )

    checked_size.unlink()
    with pytest.raises(
        EvidenceMeshError, match="cannot read Pages publication evidence"
    ):
        build_evidence_mesh(
            mesh_root,
            now=datetime(2026, 8, 26, 14, 10, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("receipt_mutation", "message"),
    [
        (
            lambda receipt: (
                receipt["workflow"].update(publication_sha="c" * 40),
                receipt["workflow"]["pages_package_job"].update(head_sha="c" * 40),
                receipt["workflow"]["pages_deploy_job"].update(head_sha="c" * 40),
                receipt["pages_artifact"].update(workflow_run_head_sha="c" * 40),
            ),
            "artifact name does not bind publication_sha",
        ),
        (
            lambda receipt: receipt["served_verification"]["resources"][0].update(
                sha256="0" * 64
            ),
            "differs from current exact bytes",
        ),
        (
            lambda receipt: receipt["served_verification"]["resources"].reverse(),
            "exact ordered required list",
        ),
        (
            lambda receipt: receipt["pages_artifact"].update(
                captured_at="2026-08-26T13:59:59Z"
            ),
            "capture clock",
        ),
        (
            lambda receipt: receipt["pages_artifact"].update(
                digest_sha256="sha256:" + "c" * 64
            ),
            "digest_sha256 must be a lowercase SHA-256",
        ),
        (
            lambda receipt: receipt["workflow"]["pages_package_job"].update(
                run_attempt=2
            ),
            "does not bind run id and attempt",
        ),
        (
            lambda receipt: receipt["workflow"]["pages_package_job"].update(
                name="Contract and unit tests"
            ),
            "name does not bind the workflow job",
        ),
        (
            lambda receipt: receipt["pages_artifact"].update(
                workflow_run_head_sha="d" * 40
            ),
            "workflow_run_head_sha does not bind publication_sha",
        ),
        (
            lambda receipt: receipt["archived_size_receipt"].update(
                workflow_run_head_sha="d" * 40
            ),
            "workflow_run_head_sha does not bind publication_sha",
        ),
        (
            lambda receipt: receipt["archived_size_receipt"].update(
                checked_in_path=(
                    "receipts/pages-artifact-size-" + PUBLICATION_SHA + ".json"
                )
            ),
            "path must exactly bind publication_sha",
        ),
        (
            lambda receipt: receipt["deployment"].update(
                state_at_verification="failure"
            ),
            "was not successful",
        ),
        (
            lambda receipt: receipt["deployment"].update(
                environment_url="https://palimpsest.info"
            ),
            "environment URL changed",
        ),
        (
            lambda receipt: receipt["deployment"].update(sha="d" * 40),
            "deployment SHA does not bind publication_sha",
        ),
    ],
)
def test_receipt_identity_resource_and_deployment_mutations_fail_closed(
    tmp_path: Path,
    receipt_mutation,
    message: str,
) -> None:
    receipt_path, size_path, _, _ = _write_evidence(
        tmp_path,
        receipt_mutation=receipt_mutation,
    )
    with pytest.raises(BriRegistryError, match=message):
        _build_descriptor(receipt_path, size_path)


@pytest.mark.parametrize(
    ("size_mutation", "canonical_size", "message"),
    [
        (
            lambda size: size.update(headroom_bytes=size["headroom_bytes"] + 1),
            True,
            "arithmetic does not reconcile",
        ),
        (
            lambda size: size.update(status="over-limit"),
            True,
            "must be within-limit",
        ),
        (
            lambda size: None,
            False,
            "must use canonical JSON bytes",
        ),
    ],
)
def test_archived_size_receipt_mutations_fail_closed(
    tmp_path: Path,
    size_mutation,
    canonical_size: bool,
    message: str,
) -> None:
    receipt_path, size_path, _, _ = _write_evidence(
        tmp_path,
        size_mutation=size_mutation,
        canonical_size=canonical_size,
    )
    with pytest.raises(BriRegistryError, match=message):
        _build_descriptor(receipt_path, size_path)


def test_receipt_byte_digest_and_current_contract_drift_fail_closed(
    tmp_path: Path,
) -> None:
    receipt_path, size_path, receipt, _ = _write_evidence(tmp_path)
    receipt["archived_size_receipt"]["sha256"] = "0" * 64
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(BriRegistryError, match="size receipt SHA-256 mismatch"):
        _build_descriptor(receipt_path, size_path)

    receipt_path, size_path, _, _ = _write_evidence(tmp_path)
    drifted_schema = tmp_path / "bri-economic-observations-v1.schema.json"
    drifted_schema.write_bytes(WDI_SCHEMA.read_bytes() + b"\n")
    with pytest.raises(BriRegistryError, match="differs from current exact bytes"):
        build_wdi_observation_descriptor(
            _live_registry(),
            bundle_path=WDI_BUNDLE,
            observation_schema_path=drifted_schema,
            series_registry_path=WDI_SERIES_REGISTRY,
            publication_receipt_path=receipt_path,
            archived_size_receipt_path=size_path,
        )


def test_served_verification_must_be_before_and_fresh_at_registry_cutoff(
    tmp_path: Path,
) -> None:
    receipt_path, size_path, _, _ = _write_evidence(
        tmp_path,
        receipt_mutation=lambda receipt: receipt["served_verification"].update(
            verified_at="2099-01-01T00:00:00Z"
        ),
    )
    with pytest.raises(BriRegistryError, match="after the trusted registry cutoff"):
        _build_descriptor(receipt_path, size_path)

    receipt_path, size_path, receipt, _ = _write_evidence(tmp_path)
    with pytest.raises(PagesPublicationReceiptError, match="stale"):
        load_pages_publication_receipt(
            receipt_path,
            archived_size_receipt_path=size_path,
            expected_dataset_id="bri-economic-context-world-bank-wdi",
            expected_source_id="world_bank_wdi",
            expected_collection_id=receipt["collection_id"],
            expected_resources=_expected_resources(),
            verification_cutoff=datetime(2026, 8, 28, 14, 5, 1, tzinfo=UTC),
        )


def test_state_combinations_fail_closed_and_repository_ready_stays_null(
    tmp_path: Path,
) -> None:
    repository_ready = build_wdi_observation_descriptor(
        _repository_ready_registry(),
        bundle_path=WDI_BUNDLE,
        observation_schema_path=WDI_SCHEMA,
        series_registry_path=WDI_SERIES_REGISTRY,
    )
    assert repository_ready["publication_state"] == "repository_ready_not_deployed"
    assert repository_ready["publication_receipt"] is None

    receipt_path, size_path, _, _ = _write_evidence(tmp_path)
    production = _build_descriptor(receipt_path, size_path)
    mutations = (
        (
            lambda row: row.update(publication_state="repository_ready_not_deployed"),
            "non-null WDI publication receipt requires production_verified",
        ),
        (
            lambda row: row.update(publication_receipt=None),
            "null WDI publication receipt requires repository_ready_not_deployed",
        ),
        (
            lambda row: row["publication_receipt"].update(status="pending"),
            "is not production_verified",
        ),
    )
    registry = _live_registry()
    for mutation, message in mutations:
        candidate = deepcopy(production)
        mutation(candidate)
        with pytest.raises(BriRegistryError, match=message):
            validate_observation_dataset_descriptor_shape(candidate, registry=registry)


def test_missing_receipt_pair_and_noncanonical_publication_bytes_fail_closed(
    tmp_path: Path,
) -> None:
    receipt_path, size_path, receipt, _ = _write_evidence(tmp_path)
    with pytest.raises(BriRegistryError, match="requires both"):
        build_wdi_observation_descriptor(
            load_registry(BRI_REGISTRY),
            bundle_path=WDI_BUNDLE,
            observation_schema_path=WDI_SCHEMA,
            series_registry_path=WDI_SERIES_REGISTRY,
            publication_receipt_path=receipt_path,
        )

    with pytest.raises(BriRegistryError, match="requires source implementation live"):
        build_wdi_observation_descriptor(
            _repository_ready_registry(),
            bundle_path=WDI_BUNDLE,
            observation_schema_path=WDI_SCHEMA,
            series_registry_path=WDI_SERIES_REGISTRY,
            publication_receipt_path=receipt_path,
            archived_size_receipt_path=size_path,
        )

    with pytest.raises(BriRegistryError, match="live WDI source requires"):
        build_wdi_observation_descriptor(
            _live_registry(),
            bundle_path=WDI_BUNDLE,
            observation_schema_path=WDI_SCHEMA,
            series_registry_path=WDI_SERIES_REGISTRY,
        )

    receipt_path.write_bytes((json.dumps(receipt, indent=2) + "\n").encode("utf-8"))
    with pytest.raises(BriRegistryError, match="must use canonical JSON bytes"):
        _build_descriptor(receipt_path, size_path)


def test_no_flag_build_auto_resolves_checked_in_release_a_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path, size_path, _, _ = _write_evidence(tmp_path)
    checked_size_path = tmp_path / SIZE_RECEIPT_PATH
    checked_size_path.parent.mkdir(parents=True)
    checked_size_path.write_bytes(size_path.read_bytes())
    live_registry_path = tmp_path / "live-bri-registry.json"
    live_registry_path.write_text(
        json.dumps(_live_registry(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bri_builder, "ROOT", tmp_path)
    monkeypatch.setattr(
        bri_builder,
        "DEFAULT_WDI_PUBLICATION_RECEIPT",
        receipt_path,
    )

    json_bytes, _ = bri_builder.build(
        live_registry_path,
        wdi_bundle_path=WDI_BUNDLE,
        wdi_observation_schema_path=WDI_SCHEMA,
        wdi_series_registry_path=WDI_SERIES_REGISTRY,
    )
    [dataset] = json.loads(json_bytes)["observation_datasets"]
    assert dataset["implementation_state"] == "live"
    assert dataset["publication_state"] == "production_verified"
    assert dataset["publication_receipt"]["release_a_sha"] == PUBLICATION_SHA


def test_no_flag_build_ignores_historical_receipt_after_source_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path, _, _, _ = _write_evidence(tmp_path)
    monkeypatch.setattr(
        bri_builder,
        "DEFAULT_WDI_PUBLICATION_RECEIPT",
        receipt_path,
    )

    json_bytes, _ = bri_builder.build(
        _repository_ready_registry_path(tmp_path),
        wdi_bundle_path=WDI_BUNDLE,
        wdi_observation_schema_path=WDI_SCHEMA,
        wdi_series_registry_path=WDI_SERIES_REGISTRY,
    )
    [dataset] = json.loads(json_bytes)["observation_datasets"]
    assert dataset["implementation_state"] == "repository_ready"
    assert dataset["publication_state"] == "repository_ready_not_deployed"
    assert dataset["publication_receipt"] is None


def test_explicit_none_keeps_repository_ready_build_receipt_free(
    tmp_path: Path,
) -> None:
    json_bytes, _ = build(
        _repository_ready_registry_path(tmp_path),
        wdi_bundle_path=WDI_BUNDLE,
        wdi_publication_receipt_path=None,
        wdi_archived_size_receipt_path=None,
    )
    [dataset] = json.loads(json_bytes)["observation_datasets"]
    assert dataset["publication_state"] == "repository_ready_not_deployed"
    assert dataset["publication_receipt"] is None


def test_frozen_v1_bytes_remain_untouched() -> None:
    frozen = FROZEN_V1.read_bytes()
    assert len(frozen) == 89_584
    assert hashlib.sha256(frozen).hexdigest() == (
        "4716ccedb6e567f0c18f9d2467e5a6fd496cad8f3e035ce47f0918698e6a690e"
    )
