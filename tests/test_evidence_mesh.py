"""Offline contract tests for the provenance-aware evidence mesh."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.evidence_mesh import (
    EvidenceMeshError,
    build_evidence_mesh,
    canonical_json_bytes,
    check_evidence_mesh,
    validate_evidence_mesh,
    write_evidence_mesh,
)
from core.lab_evidence import seal_envelope_hashes


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat(
    json.loads((ROOT / "readings/evidence-mesh-latest.json").read_text())["generated_at"]
    .replace("Z", "+00:00")
)
REQUIRED_FILES = (
    "config/evidence_mesh.json",
    "config/public_data_catalog.json",
    "readings/osint-china-latest.json",
    "integrations/intelligence-commons/manifest-v1.json",
    "integrations/intelligence-commons/narcoscope-palimpsest-v1.json",
    "integrations/intelligence-commons/narcoscope-pin-v1.json",
    "integrations/scamshield/intelligence-pack-v1.json",
)


@pytest.fixture(scope="module")
def mesh() -> dict:
    return build_evidence_mesh(ROOT, now=NOW)


def _resource(document: dict, resource_id: str) -> dict:
    return next(row for row in document["resources"] if row["resource_id"] == resource_id)


def _receipt(document: dict, input_id: str) -> dict:
    return next(row for row in document["inputs"] if row["input_id"] == input_id)


def _isolated_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in REQUIRED_FILES:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return root


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _partner_envelope() -> dict:
    source_ref = {
        "id": "pboc-release",
        "group_id": "pboc",
        "publisher": "People's Bank of China",
        "uri": "https://www.pbc.gov.cn/releases/2026-08.html",
        "retrieved_at": "2026-08-12T09:30:00Z",
        "evidence_class": "OFFICIAL_STATISTIC",
        "content_sha256": hashlib.sha256(b"pboc release").hexdigest(),
    }
    return seal_envelope_hashes({
        "schema": "lab-evidence-envelope/v1",
        "record_id": "cn.cny.loan-growth.2026-07",
        "signal_id": "cn.cny.loan-growth",
        "event_time": "2026-07-31T23:59:59Z",
        "knowledge_time": "2026-08-12T09:00:00Z",
        "publication_time": "2026-08-12T10:00:00Z",
        "jurisdiction": {
            "scheme": "ISO-3166-1-alpha-2",
            "code": "CN",
            "label": "China",
        },
        "measure": {"type": "year-on-year-change", "value": "8.7", "unit": "percent"},
        "evidence_status": "OBSERVED",
        "measured_fraction": "1",
        "support_level": "DIRECT_OBSERVATION",
        "source_groups": ["pboc"],
        "source_refs": [source_ref],
        "hashes": {
            "algorithm": "sha256",
            "record_sha256": "0" * 64,
            "source_set_sha256": "0" * 64,
        },
        "redistribution_status": "OPEN",
        "public_value_allowed": True,
        "privacy_tier": "PUBLIC_AGGREGATE",
        "review_status": "MACHINE_VALIDATED",
        "contains_exact_iocs": False,
        "contains_raw_messages": False,
        "limitations": [
            "This source-reported national aggregate does not establish causation."
        ],
        "supersedes": [],
    })


def test_schema_and_runtime_accept_the_deterministic_document(mesh: dict) -> None:
    validate_evidence_mesh(mesh)
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "protocol/evidence-mesh-v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(mesh)

    def visit(value: object, path: str = "$") -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False, path
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(schema)


def test_every_catalog_dataset_and_current_osint_signal_is_accounted_for(mesh: dict) -> None:
    catalog = json.loads((ROOT / "config/public_data_catalog.json").read_text())
    osint = json.loads((ROOT / "readings/osint-china-latest.json").read_text())
    catalog_ids = {row["id"] for row in catalog["datasets"]}
    signal_ids = {row["id"] for row in osint["signals"]}
    mesh_catalog_ids = {
        row["source_id"] for row in mesh["resources"]
        if row["project_id"] == "palimpsest" and row["namespace"] == "catalog"
    }
    mesh_signal_ids = {
        row["source_id"] for row in mesh["resources"]
        if row["project_id"] == "palimpsest" and row["namespace"] == "osint"
    }

    assert len(catalog_ids) == 54
    assert len(signal_ids) == 33
    assert mesh_catalog_ids == catalog_ids
    assert mesh_signal_ids == signal_ids
    assert mesh["summary"]["palimpsest_catalog"] == {
        "expected": 54, "accounted": 54, "complete": True,
    }
    assert mesh["summary"]["palimpsest_osint"] == {
        "expected": 33, "accounted": 33, "complete": True,
    }


def test_all_sibling_projects_have_typed_capabilities_and_input_contracts(mesh: dict) -> None:
    projects = {row["id"]: row for row in mesh["projects"]}
    assert set(projects) == {
        "palimpsest", "seiche", "liquilens", "scamshield", "narcoscope",
    }
    for project in projects.values():
        assert project["capabilities"]
        assert all(token == token.upper() and " " not in token for token in project["capabilities"])
        assert project["input_contracts"]
        assert all(contract["allowed_role"] in {
            "evidence", "context", "typology", "candidate-only",
        } for contract in project["input_contracts"])

    assert projects["seiche"]["status"] == "REVIEW_GATED"
    assert projects["seiche"]["public_url"] is None
    assert projects["liquilens"]["status"] == "REVIEW_GATED"
    assert projects["liquilens"]["public_url"] is None
    assert projects["liquilens"]["manifest_status"] == "declared"
    assert projects["scamshield"]["status"] == "ACTIVE"
    assert projects["narcoscope"]["manifest_status"] == "declared"


def test_missing_optional_snapshots_are_explicit_and_never_zero(mesh: dict) -> None:
    for input_id in ("seiche-partner-snapshot", "liquilens-partner-snapshot"):
        receipt = _receipt(mesh, input_id)
        assert receipt["required"] is False
        assert receipt["availability"] == "unavailable"
        assert receipt["resource_count"] is None
        assert receipt["sha256"] is None
        assert receipt["bytes"] is None
        assert receipt["observed_at"] is None
        placeholder = _resource(mesh, f"{receipt['project_id']}:partner:unavailable")
        assert placeholder["availability"] == "unavailable"
        assert set(placeholder["clocks"].values()) == {None}
        assert placeholder["freshness"]["status"] == "unavailable"
        assert "value" not in placeholder


def test_catalog_freshness_policy_is_strict_and_drives_resource_deadlines(
    mesh: dict, tmp_path: Path,
) -> None:
    observations = _resource(
        mesh, "palimpsest:catalog:china-economic-observations"
    )
    observed = datetime.fromisoformat(
        observations["freshness"]["observed_at"].replace("Z", "+00:00")
    )
    deadline = datetime.fromisoformat(
        observations["freshness"]["deadline"].replace("Z", "+00:00")
    )
    assert deadline - observed == timedelta(days=10)
    assert any(
        "collector" in limitation
        for limitation in observations["limitations"]
    )

    root = _isolated_root(tmp_path)
    catalog_path = root / "config/public_data_catalog.json"
    catalog = json.loads(catalog_path.read_text())
    target = next(
        row for row in catalog["datasets"]
        if row["id"] == "china-economic-observations"
    )
    target["freshness_budget"] = "P0D"
    _write_json(catalog_path, catalog)
    with pytest.raises(EvidenceMeshError, match="cadence must be positive"):
        build_evidence_mesh(root, now=NOW)

    target["freshness_budget"] = "P10D"
    target.pop("freshness_semantics")
    _write_json(catalog_path, catalog)
    with pytest.raises(EvidenceMeshError, match="must be declared together"):
        build_evidence_mesh(root, now=NOW)


def test_unverified_partner_public_endpoints_fail_config_validation(tmp_path: Path) -> None:
    root = _isolated_root(tmp_path)
    config_path = root / "config/evidence_mesh.json"
    config = json.loads(config_path.read_text())
    seiche = next(row for row in config["projects"] if row["id"] == "seiche")
    seiche["public_url"] = "https://api.seiche.info/api/v2/markets/CN-CNY/overview"
    _write_json(config_path, config)
    with pytest.raises(EvidenceMeshError, match="without a verified public data URL"):
        build_evidence_mesh(root, now=NOW)

    config = json.loads((ROOT / "config/evidence_mesh.json").read_text())
    liquilens = next(row for row in config["projects"] if row["id"] == "liquilens")
    liquilens["public_url"] = "https://github.com/beepboop2025/liquilens-lab"
    _write_json(config_path, config)
    with pytest.raises(EvidenceMeshError, match="without a verified public data URL"):
        build_evidence_mesh(root, now=NOW)


def test_mirrors_and_derived_views_do_not_manufacture_independence(mesh: dict) -> None:
    catalog = json.loads((ROOT / "config/public_data_catalog.json").read_text())
    osint = json.loads((ROOT / "readings/osint-china-latest.json").read_text())
    shared = {row["id"] for row in catalog["datasets"]} & {row["id"] for row in osint["signals"]}
    for source_id in shared:
        catalog_resource = _resource(mesh, f"palimpsest:catalog:{source_id}")
        osint_resource = _resource(mesh, f"palimpsest:osint:{source_id}")
        assert catalog_resource["independence_group"] == osint_resource["independence_group"]
        assert catalog_resource["upstream_groups"] == osint_resource["upstream_groups"]

    assert _resource(mesh, "palimpsest:catalog:ooni-gfw")["independence_group"] == (
        _resource(mesh, "palimpsest:catalog:in-path-interference")["independence_group"]
    )
    fusion = _resource(mesh, "palimpsest:osint:vantage-fusion")
    assert fusion["independence_eligible"] is False
    assert "publisher:ooni" in fusion["upstream_groups"]
    assert "publisher:censored-planet" in fusion["upstream_groups"]
    assert not any(
        group["group_id"] == fusion["independence_group"]
        for group in mesh["dependency_groups"]
    ), "a derived pipeline must not appear as an ultimate upstream group"

    for resource_id in (
        "palimpsest:catalog:evidence-mesh",
        "palimpsest:catalog:machine-investigations",
    ):
        publication_plane = _resource(mesh, resource_id)
        assert publication_plane["allowed_role"] == "context"
        assert publication_plane["independence_eligible"] is False


def test_numeric_reuse_rights_distinguish_owned_aggregate_from_link_only_source(
    mesh: dict,
) -> None:
    inside = _resource(mesh, "palimpsest:osint:inside-view")
    censored_planet = _resource(mesh, "palimpsest:osint:censored-planet")

    assert inside["rights"] == {
        "redistribution": "ATTRIBUTION_REQUIRED",
        "reuse": "derived_only",
        "training": "prohibited",
    }
    assert censored_planet["rights"] == {
        "redistribution": "LINK_ONLY",
        "reuse": "metadata_only",
        "training": "prohibited",
    }


def test_narcoscope_pin_mismatch_is_stale_not_current(tmp_path: Path) -> None:
    root = _isolated_root(tmp_path)
    path = root / "integrations/intelligence-commons/narcoscope-palimpsest-v1.json"
    artifact = json.loads(path.read_text())
    artifact["limitations"][0] += " Byte-mismatch fixture."
    _write_json(path, artifact)

    document = build_evidence_mesh(root, now=NOW)
    receipt = _receipt(document, "narcoscope-china-aggregate")
    assert receipt["availability"] == "stale"
    assert receipt["byte_identity"] == "mismatch"
    assert receipt["resource_count"] == 5
    assert "differs" in receipt["reason"]
    narco_resources = [row for row in document["resources"] if row["project_id"] == "narcoscope"]
    assert len(narco_resources) == 5
    assert {row["availability"] for row in narco_resources} == {"stale"}
    assert {row["freshness"]["status"] for row in narco_resources} == {"stale"}


def test_narcoscope_clocks_use_source_coverage_and_pin_admission(mesh: dict) -> None:
    narco = [row for row in mesh["resources"] if row["project_id"] == "narcoscope"]
    admitted_at = _receipt(mesh, "narcoscope-pin-receipt")["observed_at"]
    assert len(narco) == 5
    for resource in narco:
        assert resource["source_temporal_coverage"] is not None
        assert resource["clocks"]["knowledge_time"] == admitted_at
        assert resource["clocks"]["publication_time"] == admitted_at
        assert resource["freshness"]["observed_at"] == admitted_at
        assert resource["freshness"]["age_hours"] >= 0
        assert resource["clocks"]["event_time"] != "2026-08-12T23:59:59Z"

    retail = _resource(mesh, "narcoscope:artifact:retail-drug-prices")
    assert retail["source_temporal_coverage"] == {
        "kind": "year_range", "from_year": 2019, "to_year": 2019,
        "snapshot_date": None,
    }
    assert retail["clocks"]["event_time"] == "2019-12-31T23:59:59Z"
    ofac = _resource(mesh, "narcoscope:artifact:ofac-designations")
    assert ofac["clocks"]["event_time"] is None


def test_future_narcoscope_admission_fails_instead_of_negative_age(tmp_path: Path) -> None:
    root = _isolated_root(tmp_path)
    pin = json.loads(
        (root / "integrations/intelligence-commons/narcoscope-pin-v1.json").read_text()
    )
    admitted_at = datetime.fromisoformat(
        pin["current"]["admitted_at"].replace("Z", "+00:00")
    )
    with pytest.raises(EvidenceMeshError, match="admission is after the build time"):
        build_evidence_mesh(root, now=admitted_at - timedelta(seconds=1))


def test_naive_build_clock_is_rejected_before_timezone_conversion() -> None:
    with pytest.raises(EvidenceMeshError, match="timezone-aware"):
        build_evidence_mesh(ROOT, now=datetime(2030, 1, 2, 3, 4, 5))


def test_runtime_rejects_forged_nonnegative_age_for_future_observation(mesh: dict) -> None:
    hostile = copy.deepcopy(mesh)
    resource = next(
        row for row in hostile["resources"]
        if row["freshness"]["observed_at"] is not None
    )
    resource["freshness"]["observed_at"] = (
        NOW + timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    resource["freshness"]["age_hours"] = 0
    with pytest.raises(EvidenceMeshError, match="after mesh generation"):
        validate_evidence_mesh(hostile)


def test_valid_optional_partner_snapshot_is_admitted_as_context(tmp_path: Path) -> None:
    snapshot = tmp_path / "seiche.json"
    _write_json(snapshot, _partner_envelope())
    document = build_evidence_mesh(ROOT, now=NOW, partner_snapshot_paths={"seiche": snapshot})

    receipt = _receipt(document, "seiche-partner-snapshot")
    assert receipt["availability"] == "available"
    assert receipt["resource_count"] == 1
    resource = _resource(document, "seiche:partner:cn.cny.loan-growth.2026-07")
    assert resource["allowed_role"] == "context"
    assert resource["evidence_class"] == "OFFICIAL_STATISTIC"
    assert resource["upstream_groups"] == ["partner:seiche:pboc"]
    assert resource["independence_eligible"] is False


def test_partner_snapshot_rejects_unknown_fields_contacts_and_exact_iocs(tmp_path: Path) -> None:
    unknown = _partner_envelope()
    unknown["unknown_field"] = "not allowed"
    unknown_path = tmp_path / "unknown.json"
    _write_json(unknown_path, unknown)
    with pytest.raises(EvidenceMeshError, match="invalid seiche partner snapshot"):
        build_evidence_mesh(ROOT, now=NOW, partner_snapshot_paths={"seiche": unknown_path})

    contact = _partner_envelope()
    contact["limitations"] = ["Contact reporter@example.org for the underlying record."]
    contact = seal_envelope_hashes(contact)
    contact_path = tmp_path / "contact.json"
    _write_json(contact_path, contact)
    with pytest.raises(EvidenceMeshError, match="person-level contact"):
        build_evidence_mesh(ROOT, now=NOW, partner_snapshot_paths={"seiche": contact_path})

    exact_ioc = _partner_envelope()
    exact_ioc["contains_exact_iocs"] = True
    exact_path = tmp_path / "exact-ioc.json"
    _write_json(exact_path, exact_ioc)
    with pytest.raises(EvidenceMeshError, match="invalid seiche partner snapshot"):
        build_evidence_mesh(ROOT, now=NOW, partner_snapshot_paths={"seiche": exact_path})


def test_caller_supplied_partner_input_is_byte_bounded(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(EvidenceMeshError, match="exceeds 8388608 bytes"):
        build_evidence_mesh(ROOT, now=NOW, partner_snapshot_paths={"seiche": oversized})


def test_required_inputs_fail_closed_on_unknown_fields(tmp_path: Path) -> None:
    root = _isolated_root(tmp_path)
    manifest_path = root / "integrations/intelligence-commons/manifest-v1.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["undeclared_extension"] = True
    _write_json(manifest_path, manifest)

    with pytest.raises(EvidenceMeshError, match="unknown=.*undeclared_extension"):
        build_evidence_mesh(root, now=NOW)


def test_atomic_write_and_check_are_byte_deterministic(tmp_path: Path) -> None:
    root = _isolated_root(tmp_path)
    document = build_evidence_mesh(root, now=NOW)
    output = tmp_path / "evidence-mesh.json"
    write_evidence_mesh(document, output)

    first = output.read_bytes()
    write_evidence_mesh(copy.deepcopy(document), output)
    assert output.read_bytes() == first
    assert check_evidence_mesh(output, root=root) is True

    changed = json.loads(output.read_text())
    changed["summary"]["resource_count"] += 1
    _write_json(output, changed)
    with pytest.raises(EvidenceMeshError, match="summary totals"):
        check_evidence_mesh(output, root=root)


def test_publication_plane_payloads_cannot_feed_back_into_mesh(tmp_path: Path) -> None:
    root = _isolated_root(tmp_path)
    publication_paths = {
        "evidence-mesh": root / "readings/evidence-mesh-latest.json",
        "machine-investigations": root / "readings/machine-investigations-latest.json",
        "newsroom": root / "readings/newsroom-latest.json",
    }
    for path in publication_paths.values():
        path.unlink(missing_ok=True)
    first = build_evidence_mesh(root, now=NOW)

    for path in publication_paths.values():
        _write_json(path, {
            "schema_version": "deliberately-not-parsed",
            "generated_at": "2099-01-01T00:00:00Z",
            "payload": "downstream bytes must not enter the mesh",
        })
    second = build_evidence_mesh(root, now=NOW)

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    for dataset_id in publication_paths:
        plane = _resource(second, f"palimpsest:catalog:{dataset_id}")
        assert plane["availability"] == "available"
        assert set(plane["clocks"].values()) == {None}
        assert plane["allowed_role"] == "context"
        assert plane["independence_eligible"] is False


def test_every_resource_carries_rights_clocks_freshness_role_and_lineage(mesh: dict) -> None:
    for resource in mesh["resources"]:
        assert set(resource["rights"]) == {"redistribution", "reuse", "training"}
        assert resource["rights"]["training"] == "prohibited"
        assert set(resource["clocks"]) == {
            "event_time", "knowledge_time", "publication_time",
        }
        assert set(resource["freshness"]) == {
            "status", "observed_at", "deadline", "age_hours", "cadence",
        }
        if resource["freshness"]["age_hours"] is not None:
            assert resource["freshness"]["age_hours"] >= 0
        assert resource["allowed_role"] in {
            "evidence", "context", "typology", "candidate-only",
        }
        assert resource["independence_group"]
        assert resource["upstream_groups"]
