"""Strict admission boundary for NarcoScope's country-corridor v2 aggregate."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
COMMONS = ROOT / "integrations" / "intelligence-commons"
DEFAULT_ARTIFACT_PATH = COMMONS / "narcoscope-palimpsest-corridors-v2.json"
DEFAULT_SCHEMA_PATH = COMMONS / "narcoscope-palimpsest-corridors-v2.schema.json"
DEFAULT_RECEIPT_PATH = COMMONS / "narcoscope-corridors-pin-v2.json"
CANONICAL_URL = "https://narcoscope.com/data/narcoscope-palimpsest-corridors-v2.json"
CANONICAL_SCHEMA_URL = "https://narcoscope.com/data/narcoscope-palimpsest-corridors-v2.schema.json"
SCHEMA_VERSION = "narcoscope.palimpsest.corridor-aggregate.v2"
ARTIFACT_ID = "narcoscope.china-pakistan-myanmar.official-coverage"
RECEIPT_SCHEMA = "palimpsest-partner-pin/v2"
REPOSITORY_READY_STATUS = "repository_ready_not_deployed"
PRODUCTION_VERIFIED_STATUS = "production_verified"
PRODUCTION_VERIFICATION_CHECKS = [
    "github_deployment_success",
    "artifact_byte_identity",
    "schema_byte_identity",
    "rest_contract_v2",
    "mcp_contract_v1.1.0",
    "mcp_registry_v1.1.0",
]
EXPECTED_PRODUCTION_PROOF = {
    "repository": "beepboop2025/narcoscope",
    "commit_sha": "5bf6a31cfd98e56dadca495f35b99ecb73c1d74f",
    "deployment_id": 6103284752,
    "deployment_environment": "Production",
    "deployment_url": "https://narcoscope-4l7l78jxx-beepboop2025s-projects.vercel.app",
    "production_url": "https://narcoscope.com",
    "test_run_id": 32966260157,
    "registry_run_id": 32966416333,
    "registry_version": "1.1.0",
    "verified_at": "2026-08-26T12:03:34Z",
    "verification_checks": list(PRODUCTION_VERIFICATION_CHECKS),
}
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 20_000
MAX_HISTORY = 128

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ALLOWED_SOURCE_HOSTS = {
    "www.unodc.org", "www.incb.org", "sanctionslist.ofac.treas.gov",
    "trade.cites.org",
}
_FORBIDDEN_KEYS = {
    "entitynumber", "name", "alias", "aliases", "address", "addresses",
    "passport", "identitynumber", "dateofbirth", "latitude", "longitude",
    "coordinates", "geometry", "routeinstructions", "synthesisroute", "yield",
}
_EXPECTED_GEOGRAPHIES = [
    {"country": "China", "iso2": "CN", "iso3": "CHN"},
    {"country": "Myanmar", "iso2": "MM", "iso3": "MMR"},
    {"country": "Pakistan", "iso2": "PK", "iso3": "PAK"},
]
_EXPECTED_DATASETS = {
    "retailDrugPrices": "retail_drug_prices",
    "drugSeizures": "drug_seizures",
    "precursorCorridorIncidents": "precursor_corridor_incidents",
    "ofacDesignations": "ofac_designations",
    "wildlifeConfiscations": "wildlife_confiscations",
}


class NarcoScopeCorridorError(ValueError):
    """The v2 artifact, producer schema or pin receipt failed closed."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise NarcoScopeCorridorError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def strict_json_loads(raw: bytes, *, label: str) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        raise NarcoScopeCorridorError(f"{label} exceeds {MAX_BYTES} bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                NarcoScopeCorridorError(f"non-finite JSON number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NarcoScopeCorridorError(f"{label} is not strict UTF-8 JSON") from exc
    if type(value) is not dict:
        raise NarcoScopeCorridorError(f"{label} must be an object")
    return value


def _canonical_time(value: Any, path: str) -> datetime:
    if type(value) is not str or not _TIME_RE.fullmatch(value):
        raise NarcoScopeCorridorError(f"{path} is not canonical UTC time")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise NarcoScopeCorridorError(f"{path} is not a real UTC time") from exc


def _date(value: Any, path: str) -> date:
    if type(value) is not str:
        raise NarcoScopeCorridorError(f"{path} is not an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise NarcoScopeCorridorError(f"{path} is not a real ISO date") from exc


def _count(value: Any, path: str) -> int:
    if type(value) is not int or not 0 <= value <= 9_007_199_254_740_991:
        raise NarcoScopeCorridorError(f"{path} is not a safe nonnegative integer")
    return value


def _decimal(value: Any, path: str) -> Decimal:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise NarcoScopeCorridorError(f"{path} is not a JSON number")
    if isinstance(value, float) and not math.isfinite(value):
        raise NarcoScopeCorridorError(f"{path} is not finite")
    result = Decimal(str(value))
    if result < 0:
        raise NarcoScopeCorridorError(f"{path} is negative")
    return result


def _scan_boundary(value: Any, path: str = "artifact") -> None:
    if type(value) is dict:
        for key, item in value.items():
            if key.casefold() in _FORBIDDEN_KEYS:
                raise NarcoScopeCorridorError(f"forbidden subject or tactical field at {path}.{key}")
            _scan_boundary(item, f"{path}.{key}")
    elif type(value) is list:
        if len(value) > MAX_ROWS:
            raise NarcoScopeCorridorError(f"{path} exceeds the row bound")
        for index, item in enumerate(value):
            _scan_boundary(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise NarcoScopeCorridorError(f"non-finite number at {path}")


def validate_schema(schema: dict[str, Any]) -> dict[str, Any]:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # jsonschema exposes several schema exceptions
        raise NarcoScopeCorridorError("producer JSON Schema is invalid") from exc
    if schema.get("$id") != CANONICAL_SCHEMA_URL:
        raise NarcoScopeCorridorError("producer schema id is not canonical")
    properties = schema.get("properties")
    if type(properties) is not dict:
        raise NarcoScopeCorridorError("producer schema lacks root properties")
    if properties.get("schemaVersion", {}).get("const") != SCHEMA_VERSION:
        raise NarcoScopeCorridorError("producer schema version constant changed")
    if properties.get("artifactId", {}).get("const") != ARTIFACT_ID:
        raise NarcoScopeCorridorError("producer artifact id constant changed")
    return schema


def _provenance(dataset: dict[str, Any], path: str) -> None:
    provenance = dataset.get("provenance")
    if type(provenance) is not dict:
        raise NarcoScopeCorridorError(f"{path}.provenance is missing")
    url = provenance.get("url")
    parsed = urlsplit(url) if type(url) is str else None
    if not parsed or parsed.scheme != "https" or parsed.hostname not in _ALLOWED_SOURCE_HOSTS:
        raise NarcoScopeCorridorError(f"{path}.provenance.url is outside the allowlist")
    source_input = provenance.get("input")
    if type(source_input) is not dict or not _SHA_RE.fullmatch(str(source_input.get("sha256", ""))):
        raise NarcoScopeCorridorError(f"{path}.provenance input hash is invalid")
    _date(provenance.get("localDataDate"), f"{path}.provenance.localDataDate")


def _country_rows(value: Any, path: str) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) != len(_EXPECTED_GEOGRAPHIES):
        raise NarcoScopeCorridorError(f"{path} must contain the three target countries")
    if [row.get("geography") for row in value if type(row) is dict] != _EXPECTED_GEOGRAPHIES:
        raise NarcoScopeCorridorError(f"{path} geography identity or order changed")
    return value


def _validate_prices(dataset: dict[str, Any]) -> None:
    data = dataset["data"]
    countries = _country_rows(data.get("countries"), "retail.countries")
    total = 0
    for country in countries:
        observations = country.get("observations")
        if type(observations) is not list:
            raise NarcoScopeCorridorError("retail observations must be an array")
        count = _count(country.get("recordCount"), "retail.country.recordCount")
        if count != len(observations):
            raise NarcoScopeCorridorError("retail country recordCount does not match rows")
        if country.get("coverageStatus") != ("observed" if observations else "no_matching_rows_in_snapshot"):
            raise NarcoScopeCorridorError("retail coverage status contradicts rows")
        identities = set()
        for row in observations:
            identity = (row.get("drug"), row.get("year"))
            if identity in identities:
                raise NarcoScopeCorridorError("retail observations contain a duplicate grain")
            identities.add(identity)
            _decimal(row.get("priceUsdPerGram"), "retail.priceUsdPerGram")
            purity = row.get("purityPct")
            if purity is not None and _decimal(purity, "retail.purityPct") > 100:
                raise NarcoScopeCorridorError("retail purity exceeds 100")
        total += count
    if _count(data.get("recordCount"), "retail.recordCount") != total:
        raise NarcoScopeCorridorError("retail headline count does not reconcile")


def _sum_group(rows: Any, path: str, key: str) -> tuple[int, Decimal]:
    if type(rows) is not list:
        raise NarcoScopeCorridorError(f"{path} must be an array")
    seen = set()
    count = 0
    quantity = Decimal(0)
    for row in rows:
        identity = row.get(key)
        if identity in seen:
            raise NarcoScopeCorridorError(f"{path} contains duplicate {key}")
        seen.add(identity)
        count += _count(row.get("sourceRowCount"), f"{path}.sourceRowCount")
        quantity += _decimal(row.get("quantityKg"), f"{path}.quantityKg")
    return count, quantity


def _validate_seizures(dataset: dict[str, Any]) -> None:
    data = dataset["data"]
    countries = _country_rows(data.get("countries"), "seizures.countries")
    total_count = 0
    for country in countries:
        expected_count = _count(country.get("sourceRowCount"), "seizures.country.sourceRowCount")
        expected_quantity = _decimal(country.get("quantityKg"), "seizures.country.quantityKg")
        annual = _sum_group(country.get("byYear"), "seizures.byYear", "year")
        grouped = _sum_group(country.get("byDrugGroup"), "seizures.byDrugGroup", "drugGroup")
        if annual != (expected_count, expected_quantity) or grouped != (expected_count, expected_quantity):
            raise NarcoScopeCorridorError("seizure country totals do not reconcile")
        total_count += expected_count
    if _count(data.get("sourceRowCount"), "seizures.sourceRowCount") != total_count:
        raise NarcoScopeCorridorError("seizure headline row count does not reconcile")


def _validate_precursors(dataset: dict[str, Any]) -> None:
    data = dataset["data"]
    corridors = data.get("corridors")
    contexts = data.get("contextRecords")
    if type(corridors) is not list or type(contexts) is not list:
        raise NarcoScopeCorridorError("precursor records must be arrays")
    if _count(data.get("includedQuantitativeRecordCount"), "precursor quantitative count") != len(corridors):
        raise NarcoScopeCorridorError("precursor quantitative count does not match rows")
    if _count(data.get("includedContextRecordCount"), "precursor context count") != len(contexts):
        raise NarcoScopeCorridorError("precursor context count does not match rows")
    if data.get("crossTargetBilateralRecordCount") != 0:
        raise NarcoScopeCorridorError("candidate implies a target-country bilateral precursor record")
    aggregation = data.get("quantityAggregation")
    if type(aggregation) is not dict or aggregation.get("summedQuantityKg") is not None or aggregation.get("status") != "not_computed_mixed_claim_semantics":
        raise NarcoScopeCorridorError("mixed precursor claims must remain unsummed")
    allowed_matches = {"CHN", "MMR", "PAK"}
    for row in [*corridors, *contexts]:
        matches = row.get("geographyMatches")
        if type(matches) is not list or not matches or not set(matches) <= allowed_matches:
            raise NarcoScopeCorridorError("precursor geography matches are invalid")
    myanmar = [row for row in corridors if row.get("destination") == "Myanmar"]
    if len(myanmar) != 1 or myanmar[0].get("reportedOrigin") != "Not reported" or myanmar[0].get("geographyMatches") != ["MMR"]:
        raise NarcoScopeCorridorError("Myanmar destination lost its unreported-origin boundary")


def _validate_designations(dataset: dict[str, Any]) -> None:
    countries = _country_rows(dataset["data"].get("countries"), "designations.countries")
    for country in countries:
        total = _count(country.get("recordCount"), "designations.recordCount")
        narcotics = _count(country.get("narcoticsSpecificProgramRecordCount"), "designations.narcotics")
        tco_only = _count(country.get("tcoOnlyRecordCount"), "designations.tcoOnly")
        if total != narcotics + tco_only:
            raise NarcoScopeCorridorError("designation program counts do not partition country rows")
        expected = "observed" if total else "no_matching_rows_in_snapshot"
        if country.get("coverageStatus") != expected:
            raise NarcoScopeCorridorError("designation coverage status contradicts row count")


def _validate_wildlife(dataset: dict[str, Any]) -> None:
    countries = _country_rows(dataset["data"].get("countries"), "wildlife.countries")
    for country in countries:
        for role in ("exporterOfRecord", "importerOfRecord"):
            value = country.get(role)
            if type(value) is not dict:
                raise NarcoScopeCorridorError("wildlife role is missing")
            if value.get("coverageStatus") == "not_in_retained_top_table":
                if value.get("recordCount") is not None or value.get("rankInRetainedTable") is not None:
                    raise NarcoScopeCorridorError("unavailable wildlife row became a numeric zero")
            elif value.get("coverageStatus") == "observed":
                _count(value.get("recordCount"), "wildlife.recordCount")
                if _count(value.get("rankInRetainedTable"), "wildlife.rank") < 1:
                    raise NarcoScopeCorridorError("wildlife rank must be positive")
            else:
                raise NarcoScopeCorridorError("wildlife coverage status is invalid")


def validate_artifact(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    validate_schema(schema)
    try:
        Draft202012Validator(schema).validate(document)
    except Exception as exc:
        raise NarcoScopeCorridorError("artifact does not satisfy the pinned producer schema") from exc
    _scan_boundary(document)
    if document.get("schemaVersion") != SCHEMA_VERSION or document.get("artifactId") != ARTIFACT_ID:
        raise NarcoScopeCorridorError("artifact identity changed")
    if document.get("geographies") != _EXPECTED_GEOGRAPHIES:
        raise NarcoScopeCorridorError("artifact geography identity or order changed")
    disclosure = document.get("disclosure")
    if not isinstance(disclosure, dict) or any((
        disclosure.get("sourcePolicy") != "official_only",
        disclosure.get("illustrativeDataIncluded") is not False,
        disclosure.get("joinPolicy") != "geography_and_time_only",
        disclosure.get("politicalOrArmedActorInference") != "prohibited",
        disclosure.get("preciseCoordinateDisclosure") != "none",
    )):
        raise NarcoScopeCorridorError("artifact disclosure boundary changed")
    datasets = document.get("datasets")
    if type(datasets) is not dict or set(datasets) != set(_EXPECTED_DATASETS):
        raise NarcoScopeCorridorError("artifact dataset set changed")
    for key, expected_id in _EXPECTED_DATASETS.items():
        dataset = datasets[key]
        if dataset.get("datasetId") != expected_id or dataset.get("sourceStatus") != "official":
            raise NarcoScopeCorridorError(f"{key} identity or source status changed")
        _provenance(dataset, key)
    _validate_prices(datasets["retailDrugPrices"])
    _validate_seizures(datasets["drugSeizures"])
    _validate_precursors(datasets["precursorCorridorIncidents"])
    _validate_designations(datasets["ofacDesignations"])
    _validate_wildlife(datasets["wildlifeConfiscations"])
    components = {row.get("component") for row in document.get("exclusions", []) if type(row) is dict}
    required = {"political_and_armed_movements", "acled_conflict_rows", "designation_subject_details", "tactical_location_and_methods"}
    if not required <= components:
        raise NarcoScopeCorridorError("artifact exclusions lost a required boundary")
    _date(document.get("dataAsOf"), "dataAsOf")
    return document


def canonical_receipt_bytes(receipt: dict[str, Any]) -> bytes:
    return (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def admission_receipt(
    artifact: dict[str, Any], artifact_raw: bytes, schema_raw: bytes, *,
    admitted_at: datetime, previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = {
        "data_as_of": artifact["dataAsOf"],
        "sha256": hashlib.sha256(artifact_raw).hexdigest(),
        "schema_sha256": hashlib.sha256(schema_raw).hexdigest(),
        "admitted_at": admitted_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    superseded = list(previous.get("superseded", [])) if previous else []
    if previous and previous.get("current", {}).get("sha256") != current["sha256"]:
        old = dict(previous["current"])
        old["superseded_at"] = current["admitted_at"]
        superseded.append(old)
    if len(superseded) > MAX_HISTORY:
        raise NarcoScopeCorridorError("pin supersession history exceeds the bound")
    return {
        "schema": RECEIPT_SCHEMA,
        "producer": "narcoscope",
        "source_url": CANONICAL_URL,
        "schema_url": CANONICAL_SCHEMA_URL,
        "contract": SCHEMA_VERSION,
        "artifact_id": ARTIFACT_ID,
        "status": REPOSITORY_READY_STATUS,
        "current": current,
        "superseded": superseded,
    }


def _validate_deployment_proof(proof: Any, *, admitted_at: datetime) -> None:
    expected_fields = {
        "repository", "commit_sha", "deployment_id", "deployment_environment",
        "deployment_url", "production_url", "test_run_id", "registry_run_id",
        "registry_version", "verified_at", "verification_checks",
    }
    if type(proof) is not dict or set(proof) != expected_fields:
        raise NarcoScopeCorridorError("deployment proof fields changed")
    if proof != EXPECTED_PRODUCTION_PROOF:
        raise NarcoScopeCorridorError("deployment does not match the reviewed production proof")
    if proof["repository"] != "beepboop2025/narcoscope":
        raise NarcoScopeCorridorError("deployment proof repository changed")
    if type(proof["commit_sha"]) is not str or not _GIT_SHA_RE.fullmatch(proof["commit_sha"]):
        raise NarcoScopeCorridorError("deployment proof commit is invalid")
    for field in ("deployment_id", "test_run_id", "registry_run_id"):
        if _count(proof[field], f"deployment.{field}") == 0:
            raise NarcoScopeCorridorError(f"deployment.{field} must be positive")
    if proof["deployment_environment"] != "Production":
        raise NarcoScopeCorridorError("deployment proof is not production")
    if proof["production_url"] != "https://narcoscope.com":
        raise NarcoScopeCorridorError("deployment proof production URL changed")
    deployment_url = urlsplit(proof["deployment_url"]) if type(proof["deployment_url"]) is str else None
    if (
        not deployment_url
        or deployment_url.scheme != "https"
        or not deployment_url.hostname
        or not deployment_url.hostname.endswith(".vercel.app")
    ):
        raise NarcoScopeCorridorError("deployment proof URL is invalid")
    if proof["registry_version"] != "1.1.0":
        raise NarcoScopeCorridorError("deployment proof Registry version changed")
    verified_at = _canonical_time(proof["verified_at"], "deployment.verified_at")
    if verified_at < admitted_at:
        raise NarcoScopeCorridorError("deployment verification predates admission")
    if proof["verification_checks"] != PRODUCTION_VERIFICATION_CHECKS:
        raise NarcoScopeCorridorError("deployment verification checks changed")


def validate_receipt(receipt: dict[str, Any], *, artifact_raw: bytes, schema_raw: bytes, artifact: dict[str, Any]) -> dict[str, Any]:
    status = receipt.get("status")
    expected_root = {
        "schema", "producer", "source_url", "schema_url", "contract",
        "artifact_id", "status", "current", "superseded",
    }
    if status == PRODUCTION_VERIFIED_STATUS:
        expected_root.add("deployment")
    if set(receipt) != expected_root:
        raise NarcoScopeCorridorError("pin receipt fields changed")
    expected_identity = {
        "schema": RECEIPT_SCHEMA,
        "producer": "narcoscope",
        "source_url": CANONICAL_URL,
        "schema_url": CANONICAL_SCHEMA_URL,
        "contract": SCHEMA_VERSION,
        "artifact_id": ARTIFACT_ID,
    }
    if any(receipt.get(key) != value for key, value in expected_identity.items()):
        raise NarcoScopeCorridorError("pin receipt identity changed")
    if status not in {REPOSITORY_READY_STATUS, PRODUCTION_VERIFIED_STATUS}:
        raise NarcoScopeCorridorError("pin receipt status changed")
    current = receipt.get("current")
    if type(current) is not dict or set(current) != {"data_as_of", "sha256", "schema_sha256", "admitted_at"}:
        raise NarcoScopeCorridorError("pin receipt current fields changed")
    if current["data_as_of"] != artifact["dataAsOf"]:
        raise NarcoScopeCorridorError("pin data date does not match artifact")
    if current["sha256"] != hashlib.sha256(artifact_raw).hexdigest():
        raise NarcoScopeCorridorError("pin hash does not match artifact bytes")
    if current["schema_sha256"] != hashlib.sha256(schema_raw).hexdigest():
        raise NarcoScopeCorridorError("pin schema hash does not match schema bytes")
    admitted_at = _canonical_time(current["admitted_at"], "receipt.current.admitted_at")
    if status == PRODUCTION_VERIFIED_STATUS:
        _validate_deployment_proof(receipt["deployment"], admitted_at=admitted_at)
    if type(receipt.get("superseded")) is not list or len(receipt["superseded"]) > MAX_HISTORY:
        raise NarcoScopeCorridorError("pin supersession history is invalid")
    return receipt


def load_bundle(
    artifact_path: Path = DEFAULT_ARTIFACT_PATH,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    artifact_raw = artifact_path.read_bytes()
    schema_raw = schema_path.read_bytes()
    artifact = strict_json_loads(artifact_raw, label="NarcoScope corridor artifact")
    schema = strict_json_loads(schema_raw, label="NarcoScope corridor schema")
    validate_artifact(artifact, schema)
    receipt_raw = receipt_path.read_bytes()
    receipt = strict_json_loads(receipt_raw, label="NarcoScope corridor pin")
    if receipt_raw != canonical_receipt_bytes(receipt):
        raise NarcoScopeCorridorError("NarcoScope corridor pin is not canonical JSON")
    validate_receipt(receipt, artifact_raw=artifact_raw, schema_raw=schema_raw, artifact=artifact)
    return artifact, schema, receipt
