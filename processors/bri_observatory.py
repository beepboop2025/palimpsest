"""Validation and coverage planning for Palimpsest's Belt and Road observatory.

The observatory is a source-and-claim spine, not a database of asserted facts.
It records what a source can establish, its rights and collection state, and
which evidence fields remain missing.  This prevents a project announcement,
loan commitment, disbursement, completed asset and operating asset from being
collapsed into one misleading project count.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, time, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from collectors.bri_world_bank_wdi import load_registry as load_wdi_registry
from core.bri_observation import (
    BRIEconomicObservation,
    BRIObservationError,
    BUNDLE_SCHEMA_VERSION,
    canonical_json_bytes,
    sha256_bytes,
)


class BriRegistryError(ValueError):
    """The BRI source registry or public contract failed closed."""


WDI_DATASET_ID = "bri-economic-context-world-bank-wdi"
WDI_ARTIFACT_PATH = "readings/bri-economic-observations-latest.json"
WDI_OBSERVATION_SCHEMA_PATH = "protocol/bri-economic-observations-v1.schema.json"
WDI_SERIES_REGISTRY_PATH = "config/bri_wdi_series.json"
WDI_PUBLICATION_STATE = "repository_ready_not_deployed"
WDI_MAX_BUNDLE_BYTES = 128 * 1024 * 1024
WDI_CONTEXT_BOUNDARY = {
    "allowed_role": "context",
    "join_scope": "country_period_only",
    "project_inference": "prohibited",
    "actor_inference": "prohibited",
    "corridor_inference": "prohibited",
    "causal_inference": "prohibited",
    "tactical_data": "prohibited",
    "missing_value_policy": "source_null_remains_unavailable",
    "forecast_policy": "source_obs_status_F_remains_forecast",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WDI_BUNDLE_FIELDS = {
    "schema_version", "collection_id", "generated_at", "context_policy",
    "source", "registry_sha256", "coverage", "request_receipts",
    "observations_sha256", "observations",
}
_WDI_COVERAGE_FIELDS = {
    "start_year", "end_year", "countries", "indicators", "source_rows",
    "observed_rows", "forecast_rows", "unavailable_rows",
}
_WDI_REQUEST_RECEIPT_FIELDS = {
    "acquisition_id", "request_id", "evidence_url", "raw_response_sha256",
    "response_bytes", "source_rows", "observed_rows", "forecast_rows",
    "unavailable_rows", "dataset_last_updated", "source_release_upper_bound",
    "retrieved_at",
}
_WDI_DESCRIPTOR_FIELDS = {
    "dataset_id", "source_id", "implementation_state", "publication_state",
    "artifact", "observation_schema", "series_registry", "collection_id",
    "generated_at", "coverage", "clocks", "rights", "context_boundary",
    "publication_receipt",
}
_WDI_ARTIFACT_FIELDS = {"path", "url", "media_type", "bytes", "sha256"}
_WDI_CONTRACT_FIELDS = {"path", "url", "sha256"}
_WDI_CLOCK_FIELDS = {
    "dataset_last_updated", "source_release_upper_bound", "retrieved_at",
}
_WDI_RIGHTS_FIELDS = {
    "license", "license_url", "attribution", "redistribution_status",
    "rights_evidence_url",
}


SOURCE_CLASSES = {
    "official_china", "official_host", "multilateral", "research",
    "civil_society", "legal", "partner",
}
AUTHORITY_ROLES = {
    "primary_record", "administrative_position", "independent_observation",
    "analytical_estimate", "legal_instrument", "partner_aggregate",
}
ACCESS_MODES = {
    "open_download", "open_keyless", "public_manual", "registration",
    "licensed", "restricted",
}
RIGHTS_STATES = {
    "public_domain", "attribution", "open_reuse",
    "official_publication_review_required", "link_only_pending_review",
    "licensed_no_redistribution",
}
IMPLEMENTATION_STATES = {
    "live", "adapter_ready", "planned", "link_only", "blocked",
    "out_of_scope", "repository_ready",
}
CADENCES = {
    "event", "daily", "weekly", "monthly", "quarterly", "annual",
    "irregular", "snapshot",
}
CLAIM_CLASSES = {
    "official_statistic", "official_position", "project_register",
    "administrative_action", "allegation", "reported_event", "licensed_event",
    "modeled_estimate", "analytical_estimate", "legal_status",
    "partner_aggregate", "humanitarian_observation",
}
COVERAGE_AXES = {
    "project_identity", "project_status", "contract", "finance_commitment",
    "disbursement", "debt", "ownership", "contractor", "procurement",
    "trade", "port_logistics", "energy", "fiscal", "employment", "land",
    "compensation", "livelihood", "environment", "human_rights", "conflict",
    "elections", "legal_designation", "drug_market", "humanitarian",
    "remote_sensing", "corrections",
}
SAFE_PUBLIC_RIGHTS = {"public_domain", "attribution", "open_reuse"}
PUBLIC_BUILD_STATES = {"live", "adapter_ready", "repository_ready"}
REQUIRED_ROOT = {
    "schema_version", "as_of", "purpose", "publication_policy",
    "project_fields", "economic_metrics", "local_impact_fields",
    "movement_taxonomy", "geographies", "workstreams", "watch_targets",
    "partner_bridges", "sources",
}
REQUIRED_SOURCE = {
    "source_id", "name", "publisher", "url", "source_class",
    "authority_role", "access_mode", "rights_status", "implementation",
    "cadence", "geographies", "coverage", "independence_group",
    "claim_classes", "notes",
}


def load_registry(path: str | Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BriRegistryError(f"cannot load BRI registry: {exc}") from exc
    validate_registry(document)
    return document


def _nonempty_strings(value: Any, path: str) -> list[str]:
    if type(value) is not list or not value:
        raise BriRegistryError(f"{path} must be a non-empty array")
    if any(type(item) is not str or not item.strip() for item in value):
        raise BriRegistryError(f"{path} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise BriRegistryError(f"{path} contains duplicates")
    return value


def _https(value: Any, path: str) -> str:
    if type(value) is not str:
        raise BriRegistryError(f"{path} must be a URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise BriRegistryError(f"{path} must be a public HTTPS URL")
    return value


def validate_registry(document: Any) -> dict[str, Any]:
    if type(document) is not dict or set(document) != REQUIRED_ROOT:
        actual = set(document) if type(document) is dict else set()
        raise BriRegistryError(
            f"registry fields differ: missing={sorted(REQUIRED_ROOT - actual)}, "
            f"unknown={sorted(actual - REQUIRED_ROOT)}"
        )
    if document["schema_version"] != "palimpsest.bri-source-registry.v1":
        raise BriRegistryError("unexpected BRI registry schema")
    try:
        cutoff = datetime.fromisoformat(str(document["as_of"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BriRegistryError("as_of must be an ISO-8601 timestamp") from exc
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise BriRegistryError("as_of must include a timezone")
    project_fields = set(_nonempty_strings(document["project_fields"], "project_fields"))
    economic_metrics = set(_nonempty_strings(document["economic_metrics"], "economic_metrics"))
    impact_fields = set(_nonempty_strings(document["local_impact_fields"], "local_impact_fields"))
    required_project = {
        "identity", "host_country", "corridor", "sector", "announced_at",
        "approval_status", "contract_status", "finance_status",
        "implementation_status", "operating_status", "owner", "lender",
        "contractor", "committed_amount", "disbursed_amount", "currency",
        "source_release_at", "collected_at", "revision", "rights",
    }
    if not required_project <= project_fields:
        raise BriRegistryError(
            f"project field contract is incomplete: {sorted(required_project - project_fields)}"
        )
    if not {"jobs_promised", "jobs_observed", "land", "compensation", "grievances", "fisheries", "water"} <= impact_fields:
        raise BriRegistryError("local-impact contract is incomplete")
    if not {"commitment", "disbursement", "debt_service", "trade", "port_throughput", "fiscal_exposure"} <= economic_metrics:
        raise BriRegistryError("economic metric contract is incomplete")

    policy = document["publication_policy"]
    if type(policy) is not dict:
        raise BriRegistryError("publication_policy must be an object")
    if policy.get("project_total_rule") != "never_mix_lifecycle_states":
        raise BriRegistryError("project totals must keep lifecycle states separate")
    if policy.get("conflict_location_grain") != "delayed_administrative_area_only":
        raise BriRegistryError("conflict publication must use delayed administrative-area grain")
    if policy.get("person_level_dossiers") != "prohibited":
        raise BriRegistryError("person-level dossiers must be prohibited")

    taxonomy = document["movement_taxonomy"]
    if type(taxonomy) is not dict or taxonomy.get("umbrella_term_policy") != "concept_only_never_single_actor":
        raise BriRegistryError("Balochistan umbrella term must never be modeled as one actor")
    lanes = taxonomy.get("lanes")
    if type(lanes) is not list:
        raise BriRegistryError("movement taxonomy lanes must be an array")
    lane_ids = {lane.get("lane_id") for lane in lanes if type(lane) is dict}
    required_lanes = {
        "electoral_politics", "peaceful_civic_advocacy", "armed_organizations",
        "state_actions", "legal_designations", "rights_and_humanitarian",
        "political_economy_and_local_impact",
    }
    if not required_lanes <= lane_ids:
        raise BriRegistryError(f"movement taxonomy lacks {sorted(required_lanes - lane_ids)}")
    armed = next(lane for lane in lanes if lane.get("lane_id") == "armed_organizations")
    if "peaceful_civic_advocacy" not in armed.get("prohibited_merges", []):
        raise BriRegistryError("armed organizations must not absorb peaceful civic advocacy")

    geographies = document["geographies"]
    if type(geographies) is not list:
        raise BriRegistryError("geographies must be an array")
    geography_ids = set()
    for index, geography in enumerate(geographies):
        if type(geography) is not dict or set(geography) != {"id", "label", "kind", "parent", "priority"}:
            raise BriRegistryError(f"geographies[{index}] has an invalid shape")
        gid = geography["id"]
        if type(gid) is not str or not gid or gid in geography_ids:
            raise BriRegistryError(f"invalid duplicate geography {gid!r}")
        geography_ids.add(gid)
    required_geographies = {"GLOBAL", "CHN", "PAK", "MMR", "PAK-BAL", "PAK-GWD", "MMR-RKH"}
    if not required_geographies <= geography_ids:
        raise BriRegistryError("priority BRI geographies are incomplete")

    sources = document["sources"]
    if type(sources) is not list or not sources:
        raise BriRegistryError("sources must be a non-empty array")
    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        if type(source) is not dict or set(source) != REQUIRED_SOURCE:
            actual = set(source) if type(source) is dict else set()
            raise BriRegistryError(
                f"source {index} fields differ: missing={sorted(REQUIRED_SOURCE - actual)}, "
                f"unknown={sorted(actual - REQUIRED_SOURCE)}"
            )
        sid = source["source_id"]
        if type(sid) is not str or not sid or sid in source_ids:
            raise BriRegistryError(f"source_id must be unique: {sid!r}")
        source_ids.add(sid)
        _https(source["url"], f"{sid}.url")
        if source["source_class"] not in SOURCE_CLASSES:
            raise BriRegistryError(f"{sid}: invalid source_class")
        if source["authority_role"] not in AUTHORITY_ROLES:
            raise BriRegistryError(f"{sid}: invalid authority_role")
        if source["access_mode"] not in ACCESS_MODES:
            raise BriRegistryError(f"{sid}: invalid access_mode")
        if source["rights_status"] not in RIGHTS_STATES:
            raise BriRegistryError(f"{sid}: invalid rights_status")
        if source["implementation"] not in IMPLEMENTATION_STATES:
            raise BriRegistryError(f"{sid}: invalid implementation")
        if source["cadence"] not in CADENCES:
            raise BriRegistryError(f"{sid}: invalid cadence")
        if not set(_nonempty_strings(source["geographies"], f"{sid}.geographies")) <= geography_ids:
            raise BriRegistryError(f"{sid}: unknown geography")
        coverage = set(_nonempty_strings(source["coverage"], f"{sid}.coverage"))
        if not coverage <= COVERAGE_AXES:
            raise BriRegistryError(f"{sid}: invalid coverage {sorted(coverage - COVERAGE_AXES)}")
        claims = set(_nonempty_strings(source["claim_classes"], f"{sid}.claim_classes"))
        if not claims <= CLAIM_CLASSES:
            raise BriRegistryError(f"{sid}: invalid claim classes {sorted(claims - CLAIM_CLASSES)}")
        if not str(source["independence_group"]).strip() or not str(source["notes"]).strip():
            raise BriRegistryError(f"{sid}: independence group and notes are required")
        if source["implementation"] in PUBLIC_BUILD_STATES and source["rights_status"] not in SAFE_PUBLIC_RIGHTS:
            raise BriRegistryError(f"{sid}: build-ready source lacks public reuse rights")
        if source["access_mode"] in {"licensed", "restricted"} and source["implementation"] not in {"blocked", "out_of_scope"}:
            raise BriRegistryError(f"{sid}: licensed/restricted source must stay blocked or out of scope")
        if "allegation" in claims and source["authority_role"] == "legal_instrument":
            raise BriRegistryError(f"{sid}: allegations cannot be relabeled as legal findings")
        if "administrative_action" in claims and "legal_status" in claims:
            raise BriRegistryError(f"{sid}: administrative action and legal status must remain distinct")

    for section in ("workstreams", "watch_targets"):
        rows = document[section]
        if type(rows) is not list or not rows:
            raise BriRegistryError(f"{section} must be a non-empty array")
        ids: set[str] = set()
        key = "workstream_id" if section == "workstreams" else "target_id"
        for index, row in enumerate(rows):
            if type(row) is not dict or type(row.get(key)) is not str or row[key] in ids:
                raise BriRegistryError(f"{section}[{index}] has an invalid or duplicate id")
            ids.add(row[key])
            referenced = row.get("source_ids", [])
            if type(referenced) is not list or not referenced or not set(referenced) <= source_ids:
                raise BriRegistryError(f"{section}.{row[key]} has unknown or empty source_ids")
            required = row.get("required_coverage", [])
            if type(required) is not list or not set(required) <= COVERAGE_AXES:
                raise BriRegistryError(f"{section}.{row[key]} has invalid required_coverage")

    bridges = document["partner_bridges"]
    if type(bridges) is not list or not bridges:
        raise BriRegistryError("partner_bridges must be non-empty")
    corridor = next((item for item in bridges if item.get("contract") == "narcoscope.palimpsest.corridor-aggregate.v2"), None)
    if not corridor or corridor.get("join_policy") != "geography_and_time_only":
        raise BriRegistryError("NarcoScope v2 bridge must be geography-and-time-only")
    if corridor.get("actor_inference") != "prohibited":
        raise BriRegistryError("NarcoScope bridge actor inference must be prohibited")
    return document


def _exact_mapping(value: Any, fields: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        actual = set(value) if isinstance(value, Mapping) else set()
        raise BriRegistryError(
            f"{path} fields differ: missing={sorted(fields - actual)}, "
            f"unknown={sorted(actual - fields)}"
        )
    return value


def _sha256(value: Any, path: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise BriRegistryError(f"{path} must be a lowercase SHA-256 digest")
    return value


def _bounded_count(value: Any, path: str, *, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise BriRegistryError(f"{path} must be an integer between 0 and {maximum}")
    return value


def _canonical_utc(value: Any, path: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise BriRegistryError(f"{path} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BriRegistryError(f"{path} must be a canonical UTC timestamp") from exc
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if normalized != value:
        raise BriRegistryError(f"{path} must be a canonical UTC timestamp")
    return parsed


def _calendar_date(value: Any, path: str) -> datetime:
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise BriRegistryError(f"{path} must be an ISO calendar date")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise BriRegistryError(f"{path} must be an ISO calendar date") from exc
    return parsed


def _safe_repository_path(value: Any, path: str) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise BriRegistryError(f"{path} must be a repository-relative POSIX path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise BriRegistryError(f"{path} must be a repository-relative POSIX path")
    if str(candidate) != value:
        raise BriRegistryError(f"{path} must be a canonical repository path")
    return value


def _public_url_for(path: str) -> str:
    return f"https://palimpsest.info/{path}"


def _strict_json_bytes(raw: bytes, path: str, *, maximum: int) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        raise BriRegistryError(f"{path} is empty or exceeds {maximum} bytes")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise BriRegistryError(f"{path} is not strict UTF-8") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BriRegistryError(f"{path} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise BriRegistryError(f"{path} contains non-finite JSON number {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BriRegistryError(f"{path} is not valid JSON") from exc
    if type(value) is not dict:
        raise BriRegistryError(f"{path} must be a JSON object")
    return value


def validate_wdi_bundle(
    document: Any,
    *,
    raw: bytes,
    series_registry_path: str | Path,
) -> dict[str, Any]:
    """Validate an exact normalized WDI bundle and all of its projections.

    This is intentionally stricter than JSON Schema alone: it recomputes the
    collection identity, validates every observation identity, reconciles the
    matrix and receipt counts, and binds the checked-in series registry bytes.
    """

    bundle = dict(_exact_mapping(document, _WDI_BUNDLE_FIELDS, "WDI bundle"))
    if bundle["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise BriRegistryError("WDI bundle schema_version is unsupported")
    if canonical_json_bytes(bundle) != raw:
        raise BriRegistryError("WDI bundle must use canonical JSON bytes")

    registry_path = Path(series_registry_path)
    try:
        series_raw = registry_path.read_bytes()
        series_registry = load_wdi_registry(registry_path)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise BriRegistryError(f"cannot validate WDI series registry: {exc}") from exc
    if bundle["registry_sha256"] != sha256_bytes(series_raw):
        raise BriRegistryError("WDI bundle registry_sha256 does not match registry bytes")

    expected_policy = {
        "scope": "national_economic_context",
        "aggregate_level": "country",
        "countries": ["CHN", "MMR", "PAK"],
        "causality_boundary": "not_evidence_of_bri_causality",
        "actor_inference": "prohibited",
        "project_attribution": "prohibited",
        "tactical_data": "prohibited",
        "missing_value_policy": "source_null_remains_unavailable",
        "forecast_policy": "source_obs_status_F_remains_forecast",
        "qualification_policy": "obs_status_footnote_scale_preserved_verbatim",
        "downstream_semantics": {
            "observed": "numeric_source_value_without_forecast_marker",
            "forecast": "numeric_source_value_marked_F_not_observed",
            "unavailable": "source_null_not_zero_or_imputed",
            "join_boundary": "country_period_context_only_no_project_actor_or_causal_join",
        },
    }
    if bundle["context_policy"] != expected_policy:
        raise BriRegistryError("WDI bundle context policy was weakened or changed")

    dataset = series_registry.dataset
    expected_source = {
        "source_id": dataset["source_id"],
        "name": dataset["name"],
        "publisher": dataset["publisher"],
        "catalog_url": dataset["catalog_url"],
        "license": dataset["license"],
        "license_url": dataset["license_url"],
        "attribution": dataset["attribution"],
        "redistribution_status": dataset["redistribution_status"],
        "rights_evidence_url": dataset["rights_evidence_url"],
        "indicator_provenance_boundary": dataset["indicator_provenance_boundary"],
    }
    if bundle["source"] != expected_source:
        raise BriRegistryError("WDI bundle source or rights differ from the reviewed registry")

    coverage = _exact_mapping(bundle["coverage"], _WDI_COVERAGE_FIELDS, "WDI coverage")
    start_year = _bounded_count(coverage["start_year"], "WDI coverage.start_year", maximum=2200)
    end_year = _bounded_count(coverage["end_year"], "WDI coverage.end_year", maximum=2200)
    if not 1900 <= start_year <= end_year:
        raise BriRegistryError("WDI coverage year range is invalid")
    expected_dimensions = {
        "countries": len(series_registry.countries),
        "indicators": len(series_registry.bindings),
    }
    for key, expected in expected_dimensions.items():
        if _bounded_count(coverage[key], f"WDI coverage.{key}") != expected:
            raise BriRegistryError(f"WDI coverage {key} does not match the registry")
    for key in ("source_rows", "observed_rows", "forecast_rows", "unavailable_rows"):
        _bounded_count(coverage[key], f"WDI coverage.{key}", maximum=12_000)
    expected_rows = (end_year - start_year + 1) * coverage["countries"] * coverage["indicators"]
    if coverage["source_rows"] != expected_rows:
        raise BriRegistryError("WDI coverage does not contain the complete requested matrix")
    if coverage["source_rows"] != sum(
        coverage[key] for key in ("observed_rows", "forecast_rows", "unavailable_rows")
    ):
        raise BriRegistryError("WDI coverage evidence-state counts do not reconcile")

    receipts = bundle["request_receipts"]
    if type(receipts) is not list or len(receipts) != 1:
        raise BriRegistryError("WDI bundle must contain exactly one request receipt")
    receipt = _exact_mapping(receipts[0], _WDI_REQUEST_RECEIPT_FIELDS, "WDI request receipt")
    for field in ("acquisition_id", "request_id", "raw_response_sha256"):
        _sha256(receipt[field], f"WDI request receipt.{field}")
    _https(receipt["evidence_url"], "WDI request receipt.evidence_url")
    response_bytes = _bounded_count(
        receipt["response_bytes"], "WDI request receipt.response_bytes",
        maximum=12 * 1024 * 1024,
    )
    if response_bytes == 0:
        raise BriRegistryError("WDI request receipt response_bytes must be positive")
    for key in ("source_rows", "observed_rows", "forecast_rows", "unavailable_rows"):
        if receipt[key] != coverage[key]:
            raise BriRegistryError(f"WDI request receipt {key} does not match coverage")

    generated_at = _canonical_utc(bundle["generated_at"], "WDI bundle.generated_at")
    retrieved_at = _canonical_utc(receipt["retrieved_at"], "WDI receipt.retrieved_at")
    release_upper_bound = _canonical_utc(
        receipt["source_release_upper_bound"],
        "WDI receipt.source_release_upper_bound",
    )
    dataset_date = _calendar_date(
        receipt["dataset_last_updated"], "WDI receipt.dataset_last_updated"
    )
    if generated_at != retrieved_at:
        raise BriRegistryError("WDI bundle generated_at must equal the retrieval clock")
    if dataset_date.date() > retrieved_at.date():
        raise BriRegistryError("WDI dataset_last_updated is after retrieval")
    expected_release = min(
        datetime.combine(dataset_date.date(), time(23, 59, 59), tzinfo=timezone.utc),
        retrieved_at,
    )
    if release_upper_bound != expected_release:
        raise BriRegistryError("WDI source release upper bound does not match its clock semantics")

    observations = bundle["observations"]
    if type(observations) is not list or len(observations) != coverage["source_rows"]:
        raise BriRegistryError("WDI observation rows do not match coverage")
    if bundle["observations_sha256"] != sha256_bytes(canonical_json_bytes(observations)):
        raise BriRegistryError("WDI observations_sha256 does not bind the observation rows")
    bindings = series_registry.bindings
    expected_series = {
        binding.series_id: indicator_id for indicator_id, binding in bindings.items()
    }
    natural_keys: set[tuple[str, str, str, str]] = set()
    state_counts = Counter()
    for index, row in enumerate(observations):
        try:
            observation = BRIEconomicObservation.from_dict(row)
        except (BRIObservationError, TypeError, ValueError) as exc:
            raise BriRegistryError(f"WDI observation {index} is invalid: {exc}") from exc
        if expected_series.get(observation.series_id) != observation.indicator_id:
            raise BriRegistryError("WDI observation series does not match the registry")
        if not start_year <= observation.period_start.year <= end_year:
            raise BriRegistryError("WDI observation lies outside requested coverage")
        natural_key = (
            observation.series_id,
            observation.country_code,
            observation.period_start.isoformat(),
            observation.period_end.isoformat(),
        )
        if natural_key in natural_keys:
            raise BriRegistryError("WDI bundle contains a duplicate observation")
        natural_keys.add(natural_key)
        state_counts[observation.evidence_state] += 1
        if (
            observation.retrieved_at != retrieved_at
            or observation.source_release_upper_bound != release_upper_bound
            or observation.source_dataset_last_updated != dataset_date.date()
            or observation.request_id != receipt["request_id"]
            or observation.acquisition_id != receipt["acquisition_id"]
            or observation.raw_response_sha256 != receipt["raw_response_sha256"]
            or observation.evidence_url != receipt["evidence_url"]
        ):
            raise BriRegistryError("WDI observation is detached from the request receipt")
    if len(natural_keys) != expected_rows:
        raise BriRegistryError("WDI observation matrix is incomplete")
    expected_states = {
        "observed": coverage["observed_rows"],
        "forecast": coverage["forecast_rows"],
        "unavailable": coverage["unavailable_rows"],
    }
    if dict(state_counts) != {key: value for key, value in expected_states.items() if value}:
        raise BriRegistryError("WDI observation evidence-state counts do not reconcile")

    _sha256(bundle["collection_id"], "WDI bundle.collection_id")
    collection_payload = dict(bundle)
    collection_id = collection_payload.pop("collection_id")
    if collection_id != sha256_bytes(canonical_json_bytes(collection_payload)):
        raise BriRegistryError("WDI collection_id does not authenticate the bundle")
    return bundle


def load_wdi_bundle(
    path: str | Path,
    *,
    series_registry_path: str | Path,
) -> tuple[dict[str, Any], bytes]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise BriRegistryError(f"cannot read WDI bundle: {exc}") from exc
    document = _strict_json_bytes(raw, "WDI bundle", maximum=WDI_MAX_BUNDLE_BYTES)
    return validate_wdi_bundle(
        document,
        raw=raw,
        series_registry_path=series_registry_path,
    ), raw


def build_wdi_observation_descriptor(
    registry: dict[str, Any],
    *,
    bundle_path: str | Path,
    artifact_path: str = WDI_ARTIFACT_PATH,
    observation_schema_path: str | Path,
    observation_schema_repository_path: str = WDI_OBSERVATION_SCHEMA_PATH,
    series_registry_path: str | Path,
    series_registry_repository_path: str = WDI_SERIES_REGISTRY_PATH,
) -> dict[str, Any]:
    """Build a pre-publication descriptor bound to exact normalized bytes."""

    validate_registry(registry)
    artifact_repository_path = _safe_repository_path(artifact_path, "WDI artifact.path")
    observation_schema_repository_path = _safe_repository_path(
        observation_schema_repository_path, "WDI observation_schema.path"
    )
    series_registry_repository_path = _safe_repository_path(
        series_registry_repository_path, "WDI series_registry.path"
    )
    bundle, bundle_raw = load_wdi_bundle(
        bundle_path,
        series_registry_path=series_registry_path,
    )
    try:
        observation_schema_raw = Path(observation_schema_path).read_bytes()
        series_registry_raw = Path(series_registry_path).read_bytes()
    except OSError as exc:
        raise BriRegistryError(f"cannot read WDI contract bytes: {exc}") from exc
    schema = _strict_json_bytes(
        observation_schema_raw,
        "WDI observation schema",
        maximum=2 * 1024 * 1024,
    )
    if schema.get("$id") != _public_url_for(observation_schema_repository_path):
        raise BriRegistryError("WDI observation schema ID does not match its public path")

    source = next(
        (row for row in registry["sources"] if row["source_id"] == "world_bank_wdi"),
        None,
    )
    if source is None:
        raise BriRegistryError("BRI registry does not contain world_bank_wdi")
    implementation = source["implementation"]
    if implementation not in {"adapter_ready", "repository_ready"}:
        raise BriRegistryError("pre-proof WDI source must be adapter_ready or repository_ready")
    registry_as_of = _canonical_utc(registry["as_of"], "BRI registry.as_of")
    generated_at = _canonical_utc(bundle["generated_at"], "WDI bundle.generated_at")
    if registry_as_of < generated_at:
        raise BriRegistryError("BRI registry as_of precedes the WDI bundle retrieval clock")

    [receipt] = bundle["request_receipts"]
    rights = {
        key: bundle["source"][key] for key in sorted(_WDI_RIGHTS_FIELDS)
    }
    descriptor = {
        "dataset_id": WDI_DATASET_ID,
        "source_id": "world_bank_wdi",
        "implementation_state": implementation,
        "publication_state": WDI_PUBLICATION_STATE,
        "artifact": {
            "path": artifact_repository_path,
            "url": _public_url_for(artifact_repository_path),
            "media_type": "application/json",
            "bytes": len(bundle_raw),
            "sha256": sha256_bytes(bundle_raw),
        },
        "observation_schema": {
            "path": observation_schema_repository_path,
            "url": _public_url_for(observation_schema_repository_path),
            "sha256": sha256_bytes(observation_schema_raw),
        },
        "series_registry": {
            "path": series_registry_repository_path,
            "url": _public_url_for(series_registry_repository_path),
            "sha256": sha256_bytes(series_registry_raw),
        },
        "collection_id": bundle["collection_id"],
        "generated_at": bundle["generated_at"],
        "coverage": dict(bundle["coverage"]),
        "clocks": {
            "dataset_last_updated": receipt["dataset_last_updated"],
            "source_release_upper_bound": receipt["source_release_upper_bound"],
            "retrieved_at": receipt["retrieved_at"],
        },
        "rights": rights,
        "context_boundary": dict(WDI_CONTEXT_BOUNDARY),
        "publication_receipt": None,
    }
    validate_observation_dataset_descriptor(
        descriptor,
        registry=registry,
        artifact_raw=bundle_raw,
        artifact_document=bundle,
        observation_schema_raw=observation_schema_raw,
        series_registry_raw=series_registry_raw,
        series_registry_path=series_registry_path,
    )
    return descriptor


def validate_observation_dataset_descriptor(
    descriptor: Any,
    *,
    registry: dict[str, Any],
    artifact_raw: bytes,
    artifact_document: Any,
    observation_schema_raw: bytes,
    series_registry_raw: bytes,
    series_registry_path: str | Path,
) -> dict[str, Any]:
    """Validate a v2 observation descriptor against all bytes it advertises."""

    row = validate_observation_dataset_descriptor_shape(descriptor, registry=registry)
    artifact = row["artifact"]
    if artifact["bytes"] != len(artifact_raw) or artifact["sha256"] != sha256_bytes(artifact_raw):
        raise BriRegistryError("WDI descriptor artifact bytes or hash mismatch")

    for name, raw in (
        ("observation_schema", observation_schema_raw),
        ("series_registry", series_registry_raw),
    ):
        contract = row[name]
        if contract["sha256"] != sha256_bytes(raw):
            raise BriRegistryError(f"WDI descriptor {name} hash mismatch")
    observation_schema = _strict_json_bytes(
        observation_schema_raw,
        "WDI observation schema",
        maximum=2 * 1024 * 1024,
    )
    if observation_schema.get("$id") != _public_url_for(WDI_OBSERVATION_SCHEMA_PATH):
        raise BriRegistryError("WDI observation schema ID does not match its public path")

    bundle = validate_wdi_bundle(
        artifact_document,
        raw=artifact_raw,
        series_registry_path=series_registry_path,
    )
    if bundle["registry_sha256"] != sha256_bytes(series_registry_raw):
        raise BriRegistryError("WDI bundle and descriptor bind different series registries")
    [receipt] = bundle["request_receipts"]
    if row["collection_id"] != bundle["collection_id"] or row["generated_at"] != bundle["generated_at"]:
        raise BriRegistryError("WDI descriptor collection identity or generation clock mismatch")
    if row["coverage"] != bundle["coverage"]:
        raise BriRegistryError("WDI descriptor coverage counts mismatch the bundle")
    expected_clocks = {
        "dataset_last_updated": receipt["dataset_last_updated"],
        "source_release_upper_bound": receipt["source_release_upper_bound"],
        "retrieved_at": receipt["retrieved_at"],
    }
    if row["clocks"] != expected_clocks:
        raise BriRegistryError("WDI descriptor clocks mismatch the request receipt")
    expected_rights = {key: bundle["source"][key] for key in sorted(_WDI_RIGHTS_FIELDS)}
    if row["rights"] != expected_rights:
        raise BriRegistryError("WDI descriptor rights mismatch the bundle")
    return row


def validate_observation_dataset_descriptor_shape(
    descriptor: Any,
    *,
    registry: dict[str, Any],
) -> dict[str, Any]:
    """Validate descriptor semantics that do not require its advertised files."""

    validate_registry(registry)
    row = dict(_exact_mapping(descriptor, _WDI_DESCRIPTOR_FIELDS, "WDI descriptor"))
    if row["dataset_id"] != WDI_DATASET_ID or row["source_id"] != "world_bank_wdi":
        raise BriRegistryError("WDI descriptor identity changed")
    source = next(
        (item for item in registry["sources"] if item["source_id"] == row["source_id"]),
        None,
    )
    if source is None or row["implementation_state"] != source["implementation"]:
        raise BriRegistryError("WDI descriptor implementation state mismatches the source registry")
    if row["implementation_state"] not in {"adapter_ready", "repository_ready"}:
        raise BriRegistryError("pre-proof WDI implementation state is invalid")
    if row["publication_receipt"] is not None:
        raise BriRegistryError("pre-proof WDI publication_receipt must be null")
    if row["publication_state"] != WDI_PUBLICATION_STATE:
        raise BriRegistryError("null WDI publication receipt requires repository_ready_not_deployed")

    artifact = _exact_mapping(row["artifact"], _WDI_ARTIFACT_FIELDS, "WDI descriptor.artifact")
    artifact_path = _safe_repository_path(artifact["path"], "WDI descriptor.artifact.path")
    if artifact_path != WDI_ARTIFACT_PATH:
        raise BriRegistryError("WDI descriptor artifact path changed")
    if artifact["url"] != _public_url_for(artifact_path) or artifact["media_type"] != "application/json":
        raise BriRegistryError("WDI descriptor artifact locator or media type changed")
    if _bounded_count(artifact["bytes"], "WDI descriptor.artifact.bytes", maximum=WDI_MAX_BUNDLE_BYTES) == 0:
        raise BriRegistryError("WDI descriptor artifact must contain bytes")
    _sha256(artifact["sha256"], "WDI descriptor.artifact.sha256")

    expected_contract_paths = {
        "observation_schema": WDI_OBSERVATION_SCHEMA_PATH,
        "series_registry": WDI_SERIES_REGISTRY_PATH,
    }
    for name, expected_path in expected_contract_paths.items():
        contract = _exact_mapping(row[name], _WDI_CONTRACT_FIELDS, f"WDI descriptor.{name}")
        contract_path = _safe_repository_path(contract["path"], f"WDI descriptor.{name}.path")
        if contract_path != expected_path:
            raise BriRegistryError(f"WDI descriptor {name} path changed")
        if contract["url"] != _public_url_for(contract_path):
            raise BriRegistryError(f"WDI descriptor {name} URL does not match its path")
        _sha256(contract["sha256"], f"WDI descriptor.{name}.sha256")

    _sha256(row["collection_id"], "WDI descriptor.collection_id")
    generated_at = _canonical_utc(row["generated_at"], "WDI descriptor.generated_at")
    coverage = _exact_mapping(row["coverage"], _WDI_COVERAGE_FIELDS, "WDI descriptor.coverage")
    start_year = _bounded_count(coverage["start_year"], "WDI descriptor.coverage.start_year", maximum=2200)
    end_year = _bounded_count(coverage["end_year"], "WDI descriptor.coverage.end_year", maximum=2200)
    if not 1900 <= start_year <= end_year:
        raise BriRegistryError("WDI descriptor coverage years are invalid")
    if coverage["countries"] != 3:
        raise BriRegistryError("WDI descriptor must cover exactly three countries")
    indicators = _bounded_count(
        coverage["indicators"],
        "WDI descriptor.coverage.indicators",
        maximum=24,
    )
    if indicators == 0:
        raise BriRegistryError("WDI descriptor must contain at least one indicator")
    for key in ("source_rows", "observed_rows", "forecast_rows", "unavailable_rows"):
        _bounded_count(coverage[key], f"WDI descriptor.coverage.{key}", maximum=12_000)
    if coverage["source_rows"] != sum(
        coverage[key] for key in ("observed_rows", "forecast_rows", "unavailable_rows")
    ):
        raise BriRegistryError("WDI descriptor coverage evidence-state counts do not reconcile")
    if coverage["source_rows"] != (end_year - start_year + 1) * 3 * indicators:
        raise BriRegistryError("WDI descriptor source_rows does not match its matrix dimensions")

    clocks = _exact_mapping(row["clocks"], _WDI_CLOCK_FIELDS, "WDI descriptor.clocks")
    retrieved_at = _canonical_utc(clocks["retrieved_at"], "WDI descriptor.clocks.retrieved_at")
    release_upper_bound = _canonical_utc(
        clocks["source_release_upper_bound"],
        "WDI descriptor.clocks.source_release_upper_bound",
    )
    dataset_date = _calendar_date(
        clocks["dataset_last_updated"],
        "WDI descriptor.clocks.dataset_last_updated",
    )
    if generated_at != retrieved_at:
        raise BriRegistryError("WDI descriptor generated_at must equal retrieved_at")
    expected_release = min(
        datetime.combine(dataset_date.date(), time(23, 59, 59), tzinfo=timezone.utc),
        retrieved_at,
    )
    if release_upper_bound != expected_release:
        raise BriRegistryError("WDI descriptor release clock semantics changed")

    _exact_mapping(row["rights"], _WDI_RIGHTS_FIELDS, "WDI descriptor.rights")
    if row["rights"] != {
        "attribution": "World Bank, World Development Indicators",
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "redistribution_status": "allowed_with_attribution",
        "rights_evidence_url": (
            "https://datacatalog.worldbank.org/search/dataset/0037712/"
            "world-development-indicators"
        ),
    }:
        raise BriRegistryError("WDI descriptor rights differ from reviewed CC BY 4.0 terms")
    if row["context_boundary"] != WDI_CONTEXT_BOUNDARY:
        raise BriRegistryError("WDI descriptor context boundary was weakened or changed")
    registry_as_of = _canonical_utc(registry["as_of"], "BRI registry.as_of")
    if registry_as_of < generated_at:
        raise BriRegistryError("BRI registry as_of precedes the WDI descriptor")
    return row


def coverage_report(registry: dict[str, Any]) -> dict[str, Any]:
    validate_registry(registry)
    sources = registry["sources"]
    state_counts = Counter(source["implementation"] for source in sources)
    class_counts = Counter(source["source_class"] for source in sources)
    buildable = [source for source in sources if source["implementation"] in PUBLIC_BUILD_STATES]

    def rows_for(axis: str) -> list[dict[str, Any]]:
        return [source for source in sources if axis in source["coverage"]]

    coverage: dict[str, Any] = {}
    for axis in sorted(COVERAGE_AXES):
        matches = rows_for(axis)
        ready = [source for source in matches if source in buildable]
        coverage[axis] = {
            "sources": len(matches),
            "independent_groups": len({source["independence_group"] for source in matches}),
            "build_ready_sources": len(ready),
            "build_ready_independent_groups": len({source["independence_group"] for source in ready}),
            "source_ids": [source["source_id"] for source in matches],
        }
    geography_coverage = {}
    for geography in registry["geographies"]:
        matched = [source for source in sources if geography["id"] in source["geographies"]]
        geography_coverage[geography["id"]] = {
            "sources": len(matched),
            "independent_groups": len({source["independence_group"] for source in matched}),
            "source_ids": [source["source_id"] for source in matched],
        }
    return {
        "source_count": len(sources),
        "implementation_states": dict(sorted(state_counts.items())),
        "source_classes": dict(sorted(class_counts.items())),
        "build_ready_source_count": len(buildable),
        "coverage": coverage,
        "geographies": geography_coverage,
        "build_ready_gaps": [
            axis for axis, row in coverage.items()
            if row["build_ready_independent_groups"] == 0
        ],
    }


def ground_level_priority_adjustment(source: dict[str, Any]) -> float:
    """Return the owner-tunable public-impact component of backlog priority."""
    # Owner-tunable placeholder: these seven values define the field-impact emphasis.
    weights = {
        "compensation": 2.0, "livelihood": 2.0, "human_rights": 2.0,
        "humanitarian": 1.75, "land": 1.5, "employment": 1.25,
        "corrections": 1.0,
    }
    return sum(weights.get(axis, 0.0) for axis in source["coverage"])


def prioritized_backlog(registry: dict[str, Any]) -> list[dict[str, Any]]:
    report = coverage_report(registry)
    ready_groups = {
        axis: row["build_ready_independent_groups"]
        for axis, row in report["coverage"].items()
    }
    geography_priority = {
        geography["id"]: geography["priority"] for geography in registry["geographies"]
    }
    candidates = []
    for source in registry["sources"]:
        if source["implementation"] not in {"planned", "link_only"}:
            continue
        if source["access_mode"] in {"licensed", "restricted"}:
            continue
        marginal = sum(3 if ready_groups[axis] == 0 else 1 / (1 + ready_groups[axis]) for axis in source["coverage"])
        geo = max((geography_priority[item] for item in source["geographies"]), default=0)
        rights = 3 if source["rights_status"] in SAFE_PUBLIC_RIGHTS else 0
        cadence = {"daily": 3, "weekly": 2.75, "monthly": 2.5, "quarterly": 2, "event": 2, "annual": 1, "irregular": .5, "snapshot": .25}[source["cadence"]]
        ground_level = ground_level_priority_adjustment(source)
        candidates.append({
            "source_id": source["source_id"],
            "name": source["name"],
            "priority_score": round(marginal + geo + rights + cadence + ground_level, 3),
            "ground_level_adjustment": round(ground_level, 3),
            "rights_status": source["rights_status"],
            "geographies": source["geographies"],
            "coverage": source["coverage"],
            "next_gate": (
                "rights_review" if source["rights_status"] not in SAFE_PUBLIC_RIGHTS
                else "adapter_implementation"
            ),
        })
    return sorted(candidates, key=lambda item: (-item["priority_score"], item["source_id"]))


def build_public_artifact(
    registry: dict[str, Any],
    *,
    observation_datasets: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    validate_registry(registry)
    artifact = {
        "$schema": "/protocol/belt-and-road-observatory-v1.schema.json",
        "schema_version": "palimpsest.belt-and-road-observatory.v1",
        "as_of": registry["as_of"],
        "scope": registry["purpose"],
        "publication_policy": registry["publication_policy"],
        "coverage_report": coverage_report(registry),
        "project_fields": registry["project_fields"],
        "economic_metrics": registry["economic_metrics"],
        "local_impact_fields": registry["local_impact_fields"],
        "movement_taxonomy": registry["movement_taxonomy"],
        "geographies": registry["geographies"],
        "workstreams": registry["workstreams"],
        "watch_targets": registry["watch_targets"],
        "partner_bridges": registry["partner_bridges"],
        "sources": registry["sources"],
        "prioritized_backlog": prioritized_backlog(registry),
        "interpretation": [
            "This artifact is a coverage and provenance contract, not a claim that every listed source has been ingested.",
            "Announcement, commitment, contract, disbursement, construction, completion and operation are different lifecycle states.",
            "The Balochistan liberation movement is an umbrella research concept, never a single actor or automatic militancy label.",
            "NarcoScope contributes country aggregates only; shared geography or time does not establish an actor or causal relationship.",
        ],
    }
    if observation_datasets is None:
        return artifact
    if type(observation_datasets) not in {list, tuple} or len(observation_datasets) != 1:
        raise BriRegistryError("BRI observatory v2 requires exactly one observation dataset")
    descriptor = observation_datasets[0]
    if not isinstance(descriptor, Mapping) or descriptor.get("dataset_id") != WDI_DATASET_ID:
        raise BriRegistryError("BRI observatory v2 received an unknown observation dataset")
    descriptor = validate_observation_dataset_descriptor_shape(
        descriptor,
        registry=registry,
    )
    artifact["$schema"] = "/protocol/belt-and-road-observatory-v2.schema.json"
    artifact["schema_version"] = "palimpsest.belt-and-road-observatory.v2"
    artifact["observation_datasets"] = [descriptor]
    return artifact


def registry_projection_from_public_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Recover the source-registry fields carried verbatim by a public artifact."""

    fields = {
        "as_of": "as_of",
        "purpose": "scope",
        "publication_policy": "publication_policy",
        "project_fields": "project_fields",
        "economic_metrics": "economic_metrics",
        "local_impact_fields": "local_impact_fields",
        "movement_taxonomy": "movement_taxonomy",
        "geographies": "geographies",
        "workstreams": "workstreams",
        "watch_targets": "watch_targets",
        "partner_bridges": "partner_bridges",
        "sources": "sources",
    }
    missing = [public for public in fields.values() if public not in artifact]
    if missing:
        raise BriRegistryError(
            "BRI public artifact lacks registry projection fields: " + ", ".join(missing)
        )
    registry = {
        "schema_version": "palimpsest.bri-source-registry.v1",
        **{registry_key: artifact[public_key] for registry_key, public_key in fields.items()},
    }
    validate_registry(registry)
    return registry


def independence_collisions(registry: dict[str, Any]) -> dict[str, list[str]]:
    validate_registry(registry)
    groups: dict[str, list[str]] = defaultdict(list)
    for source in registry["sources"]:
        groups[source["independence_group"]].append(source["source_id"])
    return {
        group: sorted(source_ids)
        for group, source_ids in sorted(groups.items()) if len(source_ids) > 1
    }
