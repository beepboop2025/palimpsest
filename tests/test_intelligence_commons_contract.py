"""Offline contract guards for the public intelligence commons.

These tests intentionally use only the standard library. They validate the
machine-readable public projection and the safety properties that are easy to
lose when a UI or producer is added later; they do not fetch any linked site.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "protocol" / "lab-evidence-envelope-v1.schema.json"
PROTOCOL_PATH = ROOT / "protocol" / "lab-evidence-envelope-v1.md"
COMMONS = ROOT / "integrations" / "intelligence-commons"
MANIFEST_PATH = COMMONS / "manifest-v1.json"
README_PATH = COMMONS / "README.md"
NARCOSCOPE_PATH = COMMONS / "narcoscope-palimpsest-v1.json"
NARCOSCOPE_SCHEMA_PATH = COMMONS / "narcoscope-palimpsest-v1.schema.json"
NARCOSCOPE_PIN_PATH = COMMONS / "narcoscope-pin-v1.json"
PARTNER_PIN_SCHEMA_PATH = ROOT / "protocol" / "partner-pin-v1.schema.json"
PACK_PATH = ROOT / "integrations" / "scamshield" / "intelligence-pack-v1.json"

IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
CONTRACT = re.compile(r"^[a-z][a-z0-9.-]*(?:/v[0-9]+|\.v[0-9]+)$")
VERSION = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$")
PUBLIC_HOSTS = {
    "api.seiche.info",
    "drug-price-observatory.vercel.app",
    "narcoscope.com",
    "github.com",
    "palimpsest.info",
    "seiche.info",
}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(path: Path) -> dict:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )
    assert isinstance(value, dict), path
    return value


def _all_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)


def _assert_public_url_or_reserved_path(value: str) -> None:
    if value.startswith("/"):
        assert value == "/data/narcoscope-palimpsest-v1.json"
        return
    parsed = urlparse(value)
    assert parsed.scheme == "https", value
    assert parsed.hostname in PUBLIC_HOSTS, value
    assert parsed.username is None and parsed.password is None, value
    assert not parsed.fragment, value


def _condition_for(schema: dict, field: str, expected: object) -> dict:
    for condition in schema["allOf"]:
        branch = condition.get("if", {}).get("properties", {}).get(field, {})
        if branch.get("const") == expected:
            return condition["then"]
    raise AssertionError(f"missing conditional for {field}={expected!r}")


def test_envelope_schema_is_strict_and_refs_resolve() -> None:
    schema = _load(SCHEMA_PATH)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == (
        "https://palimpsest.info/protocol/lab-evidence-envelope-v1.schema.json"
    )
    assert schema["additionalProperties"] is False

    required = {
        "schema", "record_id", "signal_id", "event_time", "knowledge_time",
        "publication_time", "jurisdiction", "measure", "evidence_status",
        "measured_fraction", "support_level", "source_groups", "source_refs",
        "hashes", "redistribution_status", "public_value_allowed",
        "privacy_tier", "review_status", "contains_exact_iocs",
        "contains_raw_messages", "limitations", "supersedes",
    }
    assert set(schema["required"]) == required
    assert schema["properties"]["schema"]["const"] == "lab-evidence-envelope/v1"
    assert set(schema["properties"]["evidence_status"]["enum"]) == {
        "OBSERVED", "DERIVED", "SCENARIO",
    }

    definitions = schema["$defs"]
    refs = [
        value
        for value in _all_strings(schema)
        if value.startswith("#/$defs/")
    ]
    assert refs
    assert all(ref.removeprefix("#/$defs/") in definitions for ref in refs)

    for name in (
        "jurisdiction", "dimensions", "interval", "sourceRef", "method", "hashes",
    ):
        assert definitions[name]["additionalProperties"] is False
    for variant in definitions["measure"]["oneOf"]:
        assert variant["additionalProperties"] is False


def test_envelope_schema_keeps_measurement_classes_and_publication_gates_separate() -> None:
    schema = _load(SCHEMA_PATH)
    properties = schema["properties"]
    definitions = schema["$defs"]

    variants = definitions["measure"]["oneOf"]
    assert {frozenset(item["required"]) for item in variants} == {
        frozenset({"type", "value", "unit"}),
        frozenset({"type", "interval", "unit"}),
    }
    assert definitions["interval"]["required"] == ["lower", "upper", "kind"]

    observed = _condition_for(schema, "evidence_status", "OBSERVED")
    assert observed["properties"]["measured_fraction"]["const"] == "1"
    scenario = _condition_for(schema, "evidence_status", "SCENARIO")
    assert {"method"}.issubset(scenario["required"])
    assert scenario["properties"]["measured_fraction"]["const"] == "0"
    assert scenario["properties"]["support_level"]["const"] == "SCENARIO_ONLY"
    derived = _condition_for(schema, "evidence_status", "DERIVED")
    assert {"method"}.issubset(derived["required"])

    assert properties["contains_exact_iocs"]["const"] is False
    assert properties["contains_raw_messages"]["const"] is False
    assert set(properties["privacy_tier"]["enum"]) == {
        "PUBLIC_AGGREGATE", "CONTROLLED_AGGREGATE",
    }
    assert "PRIVATE" not in properties["privacy_tier"]["enum"]

    public = _condition_for(schema, "public_value_allowed", True)
    assert set(public["properties"]["redistribution_status"]["enum"]) == {
        "OPEN", "ATTRIBUTION_REQUIRED",
    }
    assert public["properties"]["privacy_tier"]["const"] == "PUBLIC_AGGREGATE"
    assert set(public["properties"]["review_status"]["enum"]) == {
        "MACHINE_VALIDATED", "HUMAN_REVIEWED",
    }

    assert definitions["sourceRef"]["required"] == [
        "id", "group_id", "publisher", "uri", "retrieved_at",
        "evidence_class", "content_sha256",
    ]
    assert definitions["hashes"]["required"] == [
        "algorithm", "record_sha256", "source_set_sha256",
    ]


def test_manifest_has_the_exact_ui_shape_and_closed_public_project_set() -> None:
    manifest = _load(MANIFEST_PATH)
    assert set(manifest) == {
        "schema", "version", "title", "scope", "claim_boundary", "projects",
        "lanes", "connections", "private_boundary", "limitations",
    }
    assert manifest["schema"] == "palimpsest-intelligence-commons-manifest/v1"
    assert VERSION.fullmatch(manifest["version"])
    assert manifest["version"] == "2026-08-12.1"
    assert manifest["title"] == "Palimpsest Intelligence Commons"

    project_fields = {
        "id", "title", "role", "status", "public_url", "data_url",
        "public_boundary",
    }
    projects = manifest["projects"]
    assert all(set(project) == project_fields for project in projects)
    assert {project["id"] for project in projects} == {
        "palimpsest", "seiche", "liquilens", "scamshield", "narcoscope",
    }
    assert {project["status"] for project in projects} == {
        "ACTIVE", "REVIEW_GATED",
    }
    assert next(
        project for project in projects if project["id"] == "liquilens"
    )["status"] == "REVIEW_GATED"
    assert all(IDENTIFIER.fullmatch(project["id"]) for project in projects)
    assert len({project["id"] for project in projects}) == len(projects)
    for project in projects:
        if project["status"] == "ACTIVE":
            _assert_public_url_or_reserved_path(project["public_url"])
            _assert_public_url_or_reserved_path(project["data_url"])
        else:
            assert project["public_url"] is None
            assert project["data_url"] is None


def test_manifest_lanes_and_typologies_have_no_dangling_references() -> None:
    manifest = _load(MANIFEST_PATH)
    pack = _load(PACK_PATH)
    project_ids = {project["id"] for project in manifest["projects"]}
    pack_typologies = {item["id"] for item in pack["typologies"]}
    lane_fields = {
        "id", "title", "question", "project_ids", "signal_ids",
        "typology_ids", "evidence_rule",
    }
    lanes = manifest["lanes"]

    assert all(set(lane) == lane_fields for lane in lanes)
    assert [lane["id"] for lane in lanes] == [
        "information-controls",
        "monetary-plumbing",
        "illicit-market-observables",
        "reviewed-laundering-scam-signals",
    ]
    for lane in lanes:
        assert IDENTIFIER.fullmatch(lane["id"])
        assert lane["project_ids"] and set(lane["project_ids"]) <= project_ids
        assert len(lane["project_ids"]) == len(set(lane["project_ids"]))
        assert lane["signal_ids"]
        assert all(IDENTIFIER.fullmatch(item) for item in lane["signal_ids"])
        assert len(lane["signal_ids"]) == len(set(lane["signal_ids"]))
        assert set(lane["typology_ids"]) <= pack_typologies
        assert len(lane["typology_ids"]) == len(set(lane["typology_ids"]))
        assert len(lane["evidence_rule"]) >= 80


def test_connections_are_directional_honest_and_reference_public_projects() -> None:
    manifest = _load(MANIFEST_PATH)
    project_ids = {project["id"] for project in manifest["projects"]}
    connection_fields = {
        "id", "from_project_id", "to_project_id", "direction", "status",
        "contract", "data_url", "claim_boundary",
    }
    connections = manifest["connections"]
    assert all(set(connection) == connection_fields for connection in connections)
    assert len({connection["id"] for connection in connections}) == len(connections)

    for connection in connections:
        assert IDENTIFIER.fullmatch(connection["id"])
        assert connection["from_project_id"] in project_ids
        assert connection["to_project_id"] in project_ids
        assert connection["from_project_id"] != connection["to_project_id"]
        assert connection["direction"] == "ONE_WAY"
        assert connection["status"] in {
            "ACTIVE", "REPOSITORY_READY", "REVIEW_GATED", "PLANNED",
        }
        assert CONTRACT.fullmatch(connection["contract"]), connection["contract"]
        if connection["data_url"] is not None:
            _assert_public_url_or_reserved_path(connection["data_url"])
        else:
            assert connection["status"] != "ACTIVE"
        assert len(connection["claim_boundary"]) >= 100

    by_id = {connection["id"]: connection for connection in connections}
    assert by_id["palimpsest-to-scamshield-typologies"]["status"] == "ACTIVE"
    assert by_id["scamshield-to-palimpsest-reviewed-assessment"]["status"] == (
        "REVIEW_GATED"
    )
    assert by_id["palimpsest-to-seiche-china-context"]["status"] == "ACTIVE"
    assert by_id["seiche-to-palimpsest-monetary-context"]["status"] == "PLANNED"
    assert by_id["liquilens-to-palimpsest-financial-context"]["status"] == (
        "REVIEW_GATED"
    )
    narco = by_id["narcoscope-to-palimpsest-public-aggregate"]
    assert narco["status"] == "ACTIVE"
    assert narco["contract"] == "narcoscope.palimpsest.china-aggregate.v1"
    assert narco["data_url"] == (
        "https://narcoscope.com/data/"
        "narcoscope-palimpsest-v1.json"
    )


def test_pinned_narcoscope_object_is_aggregate_official_and_subject_free() -> None:
    artifact = _load(NARCOSCOPE_PATH)
    schema = _load(NARCOSCOPE_SCHEMA_PATH)

    assert artifact["$schema"] == "./narcoscope-palimpsest-v1.schema.json"
    assert artifact["schemaVersion"] == "narcoscope.palimpsest.china-aggregate.v1"
    assert schema["$id"].endswith("/narcoscope-palimpsest-v1.schema.json")
    assert artifact["geography"] == {
        "country": "China", "iso2": "CN", "iso3": "CHN",
    }
    assert artifact["disclosure"] == {
        "level": "public_aggregate",
        "sourcePolicy": "official_only",
        "subjectEntityDisclosure": "none",
        "exactAddressDisclosure": "none",
        "identifierDisclosure": "none",
        "illustrativeDataIncluded": False,
        "runtimeCoupling": "none_static_artifact",
    }
    assert set(artifact["datasets"]) == {
        "retailDrugPrices",
        "drugSeizures",
        "precursorCorridorIncidents",
        "ofacDesignations",
        "wildlifeConfiscations",
    }
    for dataset in artifact["datasets"].values():
        assert dataset["sourceStatus"] == "official"
        assert dataset["limitations"]
        assert dataset["provenance"]["url"].startswith("https://")
        assert re.fullmatch(
            r"[0-9a-f]{64}", dataset["provenance"]["input"]["sha256"]
        )

    keys = {value for value in _all_strings(artifact) if isinstance(value, str)}
    assert not {
        "name", "alias", "aliases", "entityNumber", "address", "addresses",
        "identity", "identities", "wallet", "message", "messages",
    } & keys


def test_narcoscope_pin_receipt_binds_current_bytes_and_keeps_prior_revision() -> None:
    artifact_bytes = NARCOSCOPE_PATH.read_bytes()
    receipt = _load(NARCOSCOPE_PIN_PATH)
    schema = _load(PARTNER_PIN_SCHEMA_PATH)

    assert set(receipt) == {
        "schema", "producer", "source_url", "artifact_id", "current", "superseded",
    }
    assert receipt["schema"] == "palimpsest-partner-pin/v1"
    assert schema["properties"]["schema"]["const"] == receipt["schema"]
    assert receipt["current"]["data_as_of"] == "2026-08-16"
    assert receipt["current"]["sha256"] == hashlib.sha256(artifact_bytes).hexdigest()
    assert receipt["superseded"][0]["data_as_of"] == "2026-08-03"


def test_current_seiche_and_scamshield_links_are_pinned() -> None:
    manifest = _load(MANIFEST_PATH)
    projects = {project["id"]: project for project in manifest["projects"]}
    connections = {
        connection["id"]: connection for connection in manifest["connections"]
    }
    assert projects["seiche"]["status"] == "REVIEW_GATED"
    assert projects["seiche"]["public_url"] is None
    assert projects["seiche"]["data_url"] is None
    assert projects["liquilens"]["status"] == "REVIEW_GATED"
    assert projects["liquilens"]["public_url"] is None
    assert projects["liquilens"]["data_url"] is None
    assert projects["scamshield"]["public_url"] == (
        "https://github.com/beepboop2025/scamshield"
    )
    assert projects["scamshield"]["data_url"] == (
        "https://palimpsest.info/integrations/scamshield/intelligence-pack-v1.json"
    )
    assert connections["scamshield-to-palimpsest-reviewed-assessment"][
        "data_url"
    ] == "https://palimpsest.info/integrations/scamshield/intelligence-pack-v1.json"


def test_claim_and_private_boundaries_fail_closed() -> None:
    manifest = _load(MANIFEST_PATH)
    claim = manifest["claim_boundary"].lower()
    assert "descriptive context" in claim
    assert "does not establish" in claim
    assert "caused money-market stress" in claim
    assert "guilt" in claim
    assert "fused score" in claim

    private = manifest["private_boundary"].lower()
    assert "not a project in this public manifest" in private
    assert "not" in private and "dependency" in private
    assert "no private repository" in private
    assert "calibration crosses outward" in private

    limitations = " ".join(manifest["limitations"]).lower()
    assert "different sampling frames" in limitations
    assert "does not claim to show everything" in limitations
    assert "one source group" in limitations
    assert "must not be extrapolated" in limitations
    assert "preregistered hypothesis" in limitations


def test_authored_contract_has_no_local_path_or_indicator_shaped_value() -> None:
    paths = (SCHEMA_PATH, PROTOCOL_PATH, MANIFEST_PATH, README_PATH)
    authored = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    lowered = authored.lower()

    private_markers = (
        "/" + "users" + "/",
        "/" + "home" + "/",
        "file" + "://",
        ".codex" + "-worktrees",
        "black" + "-economy",
        "black" + "economy",
    )
    assert not any(marker in lowered for marker in private_markers)
    assert not re.search(r"[A-Za-z]:\\\\[^\s]", authored)

    handle = re.compile(r"(?<![A-Za-z0-9])" + chr(64) + r"[A-Za-z0-9_]{5,}")
    phone = re.compile(r"(?<![0-9])\+[0-9][0-9 -]{7,}[0-9](?![0-9])")
    wallet = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
    assert not handle.search(authored)
    assert not phone.search(authored)
    assert not wallet.search(authored)


def test_protocol_documents_rules_schema_cannot_compare_and_readme_links_resolve() -> None:
    protocol = " ".join(
        PROTOCOL_PATH.read_text(encoding="utf-8").replace("`", "").split()
    )
    for required in (
        "event_time <= knowledge_time <= publication_time",
        "lower <= upper",
        "record_id must not supersede itself",
        "Every source_refs[].group_id must occur exactly in source_groups",
        "primary endpoint and its GitHub mirror are one group",
        "must not forward-fill an annual NarcoScope observation",
        "hashes.record_sha256 has been removed",
        "does not show that one caused the other",
    ):
        assert required in protocol

    readme = README_PATH.read_text(encoding="utf-8")
    assert "/data/narcoscope-palimpsest-v1.json" in readme
    assert "python3 -m pytest -q tests/test_intelligence_commons_contract.py" in readme
    for target in re.findall(r"\]\(([^)]+)\)", readme):
        if target.startswith("https://"):
            _assert_public_url_or_reserved_path(target)
        else:
            assert (README_PATH.parent / target).resolve().is_file(), target


@pytest.mark.parametrize(
    "path",
    (
        SCHEMA_PATH, MANIFEST_PATH, PACK_PATH, NARCOSCOPE_PATH,
        NARCOSCOPE_SCHEMA_PATH, NARCOSCOPE_PIN_PATH, PARTNER_PIN_SCHEMA_PATH,
    ),
)
def test_machine_readable_inputs_reject_duplicate_keys(path: Path) -> None:
    # The helper already parsed the real files. This synthetic input proves that
    # the parser used by this contract test does not silently accept the last key.
    _load(path)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        json.loads(
            '{"schema":"first","schema":"second"}',
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
