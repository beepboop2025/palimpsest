"""Deterministic, no-network inventory of Palimpsest's evidence mesh.

The mesh is deliberately an inventory, not a fusion model.  It keeps mirrors,
derived views, and ultimate upstream publishers distinct so a second rendering
of one source cannot manufacture an independent evidence group.  Partner files
are caller-supplied, bounded, aggregate-only snapshots; absence stays visible
as ``unavailable`` and is never represented by a numeric observation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from core.lab_evidence import LabEvidenceError, validate_envelope_set
from core.narcoscope_bridge import (
    NarcoScopeBridgeError,
    validate_artifact as validate_narcoscope_artifact,
    validate_receipt as validate_narcoscope_receipt,
)
from processors.bri_observatory import (
    BriRegistryError,
    registry_projection_from_public_artifact,
    validate_observation_dataset_descriptor,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "evidence_mesh.json"
DEFAULT_OUTPUT_PATH = ROOT / "readings" / "evidence-mesh-latest.json"
SCHEMA_VERSION = "palimpsest-evidence-mesh.v1"
CONFIG_SCHEMA_VERSION = "palimpsest-evidence-mesh-config.v1"

MAX_CONFIG_BYTES = 512 * 1024
MAX_RESOURCES = 1024
MAX_PROJECTS = 16
MAX_PARTNER_RECORDS = 256
MAX_TEXT = 8192

PROJECT_IDS = ("palimpsest", "seiche", "liquilens", "scamshield", "narcoscope")
PUBLICATION_PLANE_IDS = frozenset({
    "china-article-stream",
    "china-censorship-analysis",
    "china-situation",
    "evidence-mesh",
    "machine-investigations",
    "newsroom",
})
ALLOWED_ROLES = frozenset({"evidence", "context", "typology", "candidate-only"})
AVAILABILITY = frozenset({"available", "stale", "unavailable"})
FRESHNESS = frozenset({"fresh", "stale", "unknown", "unavailable"})
EVIDENCE_CLASSES = frozenset({
    "OFFICIAL_DOCUMENT",
    "OFFICIAL_STATISTIC",
    "MARKET_OBSERVATION",
    "RESEARCH_DATASET",
    "REVIEWED_REPORT",
    "CONSENT_SCOPED_AGGREGATE",
    "METHOD_OR_ASSUMPTION",
    "DERIVED_ANALYSIS",
    "PUBLICATION_WORKFLOW",
    "INTEGRITY_PROOF",
})

_TOP_FIELDS = frozenset({
    "schema_version", "generated_at", "source", "method", "scope", "safety",
    "inputs", "projects", "resources", "dependency_groups", "summary",
})
_SAFETY_FIELDS = frozenset({
    "aggregate_only", "person_level_data", "contact_data", "exact_iocs",
    "missing_is_zero", "automatic_attribution",
})
_INPUT_RECEIPT_FIELDS = frozenset({
    "input_id", "project_id", "contract", "required", "availability", "locator",
    "public_url", "sha256", "bytes", "observed_version", "observed_at",
    "expected_sha256", "byte_identity", "resource_count", "reason",
})
_PROJECT_FIELDS = frozenset({
    "id", "title", "status", "public_url", "manifest_status", "availability",
    "capabilities", "input_contracts", "resource_ids",
})
_INPUT_CONTRACT_FIELDS = frozenset({
    "id", "contract", "transport", "required", "local_path", "public_url",
    "allowed_role",
})
_RESOURCE_FIELDS = frozenset({
    "resource_id", "project_id", "namespace", "source_id", "title", "availability",
    "allowed_role", "evidence_class", "independence_group", "upstream_groups",
    "independence_eligible", "dependency_resource_ids", "rights", "clocks",
    "freshness", "source_temporal_coverage", "contract", "public_url", "input_id",
    "limitations",
})
_RIGHTS_FIELDS = frozenset({"redistribution", "reuse", "training"})
_CLOCK_FIELDS = frozenset({"event_time", "knowledge_time", "publication_time"})
_FRESHNESS_FIELDS = frozenset({
    "status", "observed_at", "deadline", "age_hours", "cadence",
})
_SOURCE_TEMPORAL_COVERAGE_FIELDS = frozenset({
    "kind", "from_year", "to_year", "snapshot_date",
})
_GROUP_FIELDS = frozenset({
    "group_id", "resource_ids", "available_resource_count",
    "independence_eligible_resource_count",
})
_SUMMARY_FIELDS = frozenset({
    "project_count", "resource_count", "available_resource_count",
    "stale_resource_count", "unavailable_resource_count",
    "independent_groups_available", "optional_inputs_unavailable",
    "palimpsest_catalog", "palimpsest_osint", "project_resource_counts",
    "role_counts",
})
_COVERAGE_FIELDS = frozenset({"expected", "accounted", "complete"})
_COUNT_FIELDS = frozenset({"id", "count"})

_CONFIG_FIELDS = frozenset({
    "schema_version", "max_input_bytes", "projects", "derived_dependencies",
    "narcoscope_pin",
})
_CONFIG_PROJECT_FIELDS = frozenset({
    "id", "title", "status", "public_url", "capabilities", "input_contracts",
})
_NARCOSCOPE_PIN_FIELDS = frozenset({"freshness_days"})

_CATALOG_TOP_FIELDS = frozenset({"schema_version", "catalog", "datasets"})
_CATALOG_FIELDS = frozenset({
    "id", "name", "description", "cadence_semantics", "publisher", "homepage",
    "contact",
})
_CATALOG_DATASET_REQUIRED = frozenset({
    "id", "name", "description", "layer", "stage", "collection_mode", "status",
    "cadence", "geography", "sources", "latest", "landing_page", "method",
    "count_fields", "license",
})
_CATALOG_DATASET_ALLOWED = _CATALOG_DATASET_REQUIRED | {
    "history", "freshness_budget", "freshness_semantics", "publication_allowed",
}

_OSINT_TOP_FIELDS = frozenset({
    "alerts", "generated_at", "headline", "health", "input_commit", "layers",
    "method", "method_version", "n_signals_live", "n_signals_reporting",
    "n_signals_total", "schema_version", "scope", "signals", "source",
})
_OSINT_SIGNAL_FIELDS = frozenset({
    "cadence_hours", "freshness_deadline", "health", "id", "input", "layer",
    "live", "method", "method_version", "metric", "optional", "payload",
    "payload_complete", "raw_url",
    "scope", "source", "source_timestamp", "status", "summary", "title",
})
_OSINT_HEALTH_FIELDS = frozenset({
    "ok", "reason", "age_hours", "upstream_status", "collector_status",
    "collector_reason", "pipeline_checked_at",
})
_OSINT_INPUT_FIELDS = frozenset({"filename", "sha256", "bytes"})
_OSINT_LAYER_FIELDS = frozenset({
    "id", "title", "n_total", "n_reporting", "n_live", "n_degraded", "status",
    "signal_ids",
})
_OSINT_ALERT_FIELDS = frozenset({"id", "kind", "severity", "title", "summary", "source_id"})
_OSINT_TOP_HEALTH_FIELDS = frozenset({
    "counts", "live_definition", "reporting_definition", "required_live",
    "required_reporting", "required_total", "status",
})
_OSINT_STATUS_COUNT_FIELDS = frozenset({"corrupt", "degraded", "live", "missing", "stale"})
_OSINT_METRIC_FIELDS = frozenset({"label", "value", "unit", "denominator"})
_OSINT_DENOMINATOR_FIELDS = frozenset({"label", "value"})

_MANIFEST_TOP_FIELDS = frozenset({
    "schema", "version", "title", "scope", "claim_boundary", "projects", "lanes",
    "connections", "private_boundary", "limitations",
})
_MANIFEST_PROJECT_FIELDS = frozenset({
    "id", "title", "role", "status", "public_url", "data_url", "public_boundary",
})
_MANIFEST_LANE_FIELDS = frozenset({
    "id", "title", "question", "project_ids", "signal_ids", "typology_ids",
    "evidence_rule",
})
_MANIFEST_CONNECTION_FIELDS = frozenset({
    "id", "from_project_id", "to_project_id", "direction", "status", "contract",
    "data_url", "claim_boundary",
})

_PACK_TOP_FIELDS = frozenset({
    "schema", "version", "generated_at", "publisher", "method", "sources", "typologies",
})
_PACK_SOURCE_FIELDS = frozenset({"id", "publisher", "title", "published_at", "url"})
_PACK_TYPOLOGY_FIELDS = frozenset({
    "id", "dimension", "label", "description", "minimum_indicators",
    "minimum_specificity", "source_refs", "limitations", "indicators",
})
_PACK_INDICATOR_ALLOWED = frozenset({
    "id", "label", "evidence_class", "specificity", "any_terms", "any_signals",
    "all_signals",
})
_PACK_INDICATOR_REQUIRED = frozenset({"id", "label", "evidence_class", "specificity"})

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_DURATION_RE = re.compile(
    r"^P(?:(?P<years>[0-9]+)Y)?(?:(?P<months>[0-9]+)M)?"
    r"(?:(?P<weeks>[0-9]+)W)?(?:(?P<days>[0-9]+)D)?"
    r"(?:T(?:(?P<hours>[0-9]+(?:\.[0-9]+)?)H)?)?$"
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){9,}(?!\d)")
_PROHIBITED_KEYS = frozenset({
    "person", "person_id", "person_name", "individual", "individual_id",
    "individual_name", "respondent", "respondent_id", "respondent_name", "email",
    "email_address", "phone", "phone_number", "home_address", "device_id", "handle",
    "wallet", "wallet_address", "account_id", "exact_ioc", "ioc", "iocs", "ioc_value",
    "raw_message", "raw_messages",
})


class EvidenceMeshError(ValueError):
    """An input or output violated the evidence-mesh contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize stable finite JSON with a final newline."""

    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceMeshError("document is not finite canonical JSON") from exc


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceMeshError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    raise EvidenceMeshError(f"non-finite JSON number: {token}")


def _read_bounded(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceMeshError(f"cannot safely open input: {path.name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceMeshError(f"input is not a regular file: {path.name}")
        if metadata.st_size > maximum:
            raise EvidenceMeshError(f"input exceeds {maximum} bytes: {path.name}")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            part = os.read(descriptor, min(1024 * 1024, remaining))
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            raise EvidenceMeshError(f"input changed while being read: {path.name}")
        return raw
    finally:
        os.close(descriptor)


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except EvidenceMeshError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceMeshError(f"invalid UTF-8 JSON: {label}") from exc


def _load_json(path: Path, maximum: int) -> tuple[Any, bytes]:
    raw = _read_bounded(path, maximum)
    return _decode_json(raw, path.name), raw


def _require_exact(value: Any, fields: frozenset[str], path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise EvidenceMeshError(f"{path} must be an object")
    actual = set(value)
    if actual != fields:
        raise EvidenceMeshError(
            f"{path} fields do not match contract "
            f"(missing={sorted(fields - actual)}, unknown={sorted(actual - fields)})"
        )
    return value


def _require_allowed(
    value: Any, required: frozenset[str], allowed: frozenset[str], path: str
) -> dict[str, Any]:
    if type(value) is not dict:
        raise EvidenceMeshError(f"{path} must be an object")
    actual = set(value)
    if not required <= actual or not actual <= allowed:
        raise EvidenceMeshError(
            f"{path} fields do not match contract "
            f"(missing={sorted(required - actual)}, unknown={sorted(actual - allowed)})"
        )
    return value


def _text(value: Any, path: str, maximum: int = MAX_TEXT) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise EvidenceMeshError(f"{path} must be bounded non-empty text")
    return value


def _nullable_text(value: Any, path: str, maximum: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    return _text(value, path, maximum)


def _identifier(value: Any, path: str) -> str:
    text = _text(value, path, 160)
    if not _ID_RE.fullmatch(text):
        raise EvidenceMeshError(f"{path} is not a stable identifier")
    return text


def _safe_count(value: Any, path: str) -> int:
    if type(value) is not int or value < 0 or value > 9_007_199_254_740_991:
        raise EvidenceMeshError(f"{path} is not a nonnegative safe integer")
    return value


def _url(
    value: Any, path: str, *, nullable: bool = False,
    allow_fragment: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    text = _text(value, path, 2048)
    parsed = urlsplit(text)
    try:
        port = parsed.port
    except ValueError as exc:
        raise EvidenceMeshError(f"{path} has an invalid port") from exc
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or port is not None
        or (parsed.fragment and not allow_fragment)
        or (
            parsed.fragment
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", parsed.fragment)
            is None
        )
    ):
        raise EvidenceMeshError(f"{path} must be a plain HTTPS URL")
    return text


def _parse_timestamp(value: Any, path: str) -> datetime:
    text = _text(value, path, 64)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceMeshError(f"{path} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceMeshError(f"{path} lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_or_date(value: Any, path: str) -> datetime:
    if type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.combine(date.fromisoformat(value), time.max, timezone.utc)
        except ValueError as exc:
            raise EvidenceMeshError(f"{path} is not a real date") from exc
    return _parse_timestamp(value, path)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration(value: str, path: str) -> timedelta:
    match = _DURATION_RE.fullmatch(value)
    if not match or not any(match.groupdict().values()):
        raise EvidenceMeshError(f"{path} is not a supported ISO cadence")
    # Calendar months/years do not have a fixed duration.  The mesh uses a
    # conservative 30/365-day freshness window only; it never rewrites source
    # event clocks with this approximation.
    days = (
        365 * int(match.group("years") or 0)
        + 30 * int(match.group("months") or 0)
        + 7 * int(match.group("weeks") or 0)
        + int(match.group("days") or 0)
    )
    hours = float(match.group("hours") or 0)
    result = timedelta(days=days, hours=hours)
    if result <= timedelta(0):
        raise EvidenceMeshError(f"{path} cadence must be positive")
    return result


def _repo_path(root: Path, value: Any, path: str) -> Path:
    relative = Path(_text(value, path, 512))
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceMeshError(f"{path} must be a repository-relative path")
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise EvidenceMeshError(f"{path} escapes repository root")
    return candidate


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise EvidenceMeshError("cannot derive an upstream group from empty source text")
    return slug[:120].rstrip("-")


def _scan_aggregate_only(value: Any, path: str = "$") -> None:
    """Reject person-level/contact/IOC fields on imported partner data and output."""

    if type(value) is dict:
        for key, child in value.items():
            lowered = key.casefold()
            if lowered in _PROHIBITED_KEYS:
                raise EvidenceMeshError(f"prohibited person/contact/IOC field at {path}.{key}")
            _scan_aggregate_only(child, f"{path}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _scan_aggregate_only(child, f"{path}[{index}]")
    elif type(value) is str and not value.startswith(("https://", "urn:")):
        leaf = path.rsplit(".", 1)[-1].casefold()
        machine = any(
            token in leaf
            for token in ("hash", "sha", "_id", "_time", "_at", "version", "contract", "cadence")
        )
        if not machine and (_EMAIL_RE.search(value) or _PHONE_RE.search(value)):
            raise EvidenceMeshError(f"possible person-level contact at {path}")
    elif type(value) is float and not math.isfinite(value):
        raise EvidenceMeshError(f"non-finite number at {path}")


def _validate_config(config: Any) -> dict[str, Any]:
    config = _require_exact(config, _CONFIG_FIELDS, "config")
    if config["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise EvidenceMeshError("unknown evidence-mesh config schema")
    maximum = _safe_count(config["max_input_bytes"], "config.max_input_bytes")
    if not 1024 <= maximum <= 64 * 1024 * 1024:
        raise EvidenceMeshError("config.max_input_bytes is outside the bounded range")
    projects = config["projects"]
    if type(projects) is not list or not 1 <= len(projects) <= MAX_PROJECTS:
        raise EvidenceMeshError("config.projects must be a bounded array")
    ids: list[str] = []
    contract_ids: set[str] = set()
    for index, project in enumerate(projects):
        project = _require_exact(project, _CONFIG_PROJECT_FIELDS, f"config.projects[{index}]")
        project_id = _identifier(project["id"], f"config.projects[{index}].id")
        ids.append(project_id)
        _text(project["title"], f"config.projects[{index}].title", 128)
        if project["status"] not in {"ACTIVE", "REPOSITORY_READY", "REVIEW_GATED", "PLANNED"}:
            raise EvidenceMeshError("unknown project status")
        _url(project["public_url"], f"config.projects[{index}].public_url", nullable=True)
        capabilities = project["capabilities"]
        if (
            type(capabilities) is not list or not capabilities or len(capabilities) > 32
            or len(capabilities) != len(set(capabilities))
        ):
            raise EvidenceMeshError("project capabilities must be a bounded unique array")
        for capability in capabilities:
            if type(capability) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", capability):
                raise EvidenceMeshError("project capability is not a typed token")
        contracts = project["input_contracts"]
        if type(contracts) is not list or not contracts or len(contracts) > 16:
            raise EvidenceMeshError("project input contracts must be a bounded array")
        for c_index, contract in enumerate(contracts):
            location = f"config.projects[{index}].input_contracts[{c_index}]"
            contract = _require_exact(contract, _INPUT_CONTRACT_FIELDS, location)
            input_id = _identifier(contract["id"], f"{location}.id")
            if input_id in contract_ids:
                raise EvidenceMeshError(f"duplicate input contract id: {input_id}")
            contract_ids.add(input_id)
            _text(contract["contract"], f"{location}.contract", 128)
            if contract["transport"] not in {
                "LOCAL_FILE", "LOCAL_PINNED_MIRROR", "PUBLIC_HTTPS_DECLARATION",
                "CALLER_SUPPLIED_LOCAL_FILE",
            }:
                raise EvidenceMeshError("unknown input transport")
            if type(contract["required"]) is not bool:
                raise EvidenceMeshError("input contract required must be boolean")
            if contract["local_path"] is not None:
                relative = Path(_text(contract["local_path"], f"{location}.local_path", 512))
                if relative.is_absolute() or ".." in relative.parts:
                    raise EvidenceMeshError("configured local path must be repository-relative")
            _url(contract["public_url"], f"{location}.public_url", nullable=True)
            if contract["allowed_role"] not in ALLOWED_ROLES:
                raise EvidenceMeshError("unknown input allowed role")
    if ids != list(PROJECT_IDS):
        raise EvidenceMeshError(f"config project order must be {list(PROJECT_IDS)}")
    by_id = {project["id"]: project for project in projects}
    for project_id in ("seiche", "liquilens"):
        project = by_id[project_id]
        expected_contract = f"{project_id}-partner-snapshot"
        if (
            project["status"] != "REVIEW_GATED"
            or project["public_url"] is not None
            or len(project["input_contracts"]) != 1
            or project["input_contracts"][0] != {
                "id": expected_contract,
                "contract": "lab-evidence-envelope/v1",
                "transport": "CALLER_SUPPLIED_LOCAL_FILE",
                "required": False,
                "local_path": None,
                "public_url": None,
                "allowed_role": "context",
            }
        ):
            raise EvidenceMeshError(
                f"{project_id} must remain a review-gated caller-supplied contract "
                "without a verified public data URL"
            )

    dependencies = config["derived_dependencies"]
    if type(dependencies) is not dict or len(dependencies) > 128:
        raise EvidenceMeshError("derived_dependencies must be a bounded object")
    for key, values in dependencies.items():
        _identifier(key, f"config.derived_dependencies.{key}")
        if (
            type(values) is not list or not values or len(values) > 64
            or len(values) != len(set(values))
        ):
            raise EvidenceMeshError("derived dependency values must be bounded and unique")
        for value in values:
            _identifier(value, f"config.derived_dependencies.{key}[]")

    pin = _require_exact(config["narcoscope_pin"], _NARCOSCOPE_PIN_FIELDS, "config.narcoscope_pin")
    freshness_days = _safe_count(pin["freshness_days"], "config.narcoscope_pin.freshness_days")
    if not 1 <= freshness_days <= 3650:
        raise EvidenceMeshError("NarcoScope freshness window is outside policy bounds")
    return config


def _validate_catalog(value: Any) -> dict[str, Any]:
    value = _require_exact(value, _CATALOG_TOP_FIELDS, "catalog")
    if value["schema_version"] != "1.0.0":
        raise EvidenceMeshError("unknown public data catalog schema")
    _require_exact(value["catalog"], _CATALOG_FIELDS, "catalog.catalog")
    datasets = value["datasets"]
    if type(datasets) is not list or not datasets or len(datasets) > 256:
        raise EvidenceMeshError("catalog.datasets must be a bounded array")
    ids: set[str] = set()
    for index, dataset in enumerate(datasets):
        path = f"catalog.datasets[{index}]"
        dataset = _require_allowed(dataset, _CATALOG_DATASET_REQUIRED, _CATALOG_DATASET_ALLOWED, path)
        dataset_id = _identifier(dataset["id"], f"{path}.id")
        if dataset_id in ids:
            raise EvidenceMeshError(f"duplicate catalog dataset: {dataset_id}")
        ids.add(dataset_id)
        for key in ("name", "description", "layer", "stage", "collection_mode", "status", "cadence", "latest", "landing_page", "method"):
            _text(dataset[key], f"{path}.{key}")
        publication_allowed = dataset.get("publication_allowed", True)
        if type(publication_allowed) is not bool:
            raise EvidenceMeshError(f"{path}.publication_allowed must be boolean")
        if publication_allowed is False and dataset["status"] != "gated":
            raise EvidenceMeshError(
                f"{path} must be gated when publication_allowed is false"
            )
        _duration(dataset["cadence"], f"{path}.cadence")
        has_budget = "freshness_budget" in dataset
        has_semantics = "freshness_semantics" in dataset
        if has_budget != has_semantics:
            raise EvidenceMeshError(
                f"{path}.freshness_budget and freshness_semantics must be declared together"
            )
        if has_budget:
            _duration(dataset["freshness_budget"], f"{path}.freshness_budget")
            _text(dataset["freshness_semantics"], f"{path}.freshness_semantics", 1024)
        for field in ("geography", "sources", "count_fields"):
            items = dataset[field]
            if type(items) is not list or len(items) > 64 or len(items) != len(set(items)):
                raise EvidenceMeshError(f"{path}.{field} must be a bounded unique array")
            for item in items:
                _text(item, f"{path}.{field}[]", 256)
        if not dataset["sources"]:
            raise EvidenceMeshError(f"{path}.sources cannot be empty")
        license_value = _require_exact(
            dataset["license"], frozenset({"name", "url"}), f"{path}.license"
        )
        _text(license_value["name"], f"{path}.license.name", 512)
        _url(
            license_value["url"], f"{path}.license.url", allow_fragment=True
        )
    return value


def _validate_osint(value: Any) -> dict[str, Any]:
    value = _require_exact(value, _OSINT_TOP_FIELDS, "osint")
    if value["schema_version"] != "osint-china.v1":
        raise EvidenceMeshError("unknown OSINT command-surface schema")
    _parse_timestamp(value["generated_at"], "osint.generated_at")
    signals = value["signals"]
    if type(signals) is not list or not signals or len(signals) > 256:
        raise EvidenceMeshError("osint.signals must be a bounded array")
    ids: set[str] = set()
    for index, signal in enumerate(signals):
        path = f"osint.signals[{index}]"
        signal = _require_exact(signal, _OSINT_SIGNAL_FIELDS, path)
        signal_id = _identifier(signal["id"], f"{path}.id")
        if signal_id in ids:
            raise EvidenceMeshError(f"duplicate OSINT signal: {signal_id}")
        ids.add(signal_id)
        _require_exact(signal["health"], _OSINT_HEALTH_FIELDS, f"{path}.health")
        input_value = _require_exact(signal["input"], _OSINT_INPUT_FIELDS, f"{path}.input")
        if input_value["sha256"] is not None and (
            type(input_value["sha256"]) is not str or not _SHA_RE.fullmatch(input_value["sha256"])
        ):
            raise EvidenceMeshError(f"{path}.input.sha256 is invalid")
        if input_value["bytes"] is not None:
            _safe_count(input_value["bytes"], f"{path}.input.bytes")
        if type(signal["live"]) is not bool or type(signal["optional"]) is not bool:
            raise EvidenceMeshError(f"{path} live/optional must be boolean")
        if type(signal["payload_complete"]) is not bool:
            raise EvidenceMeshError(f"{path}.payload_complete must be boolean")
        if signal["payload"] is None and signal["payload_complete"] is not False:
            raise EvidenceMeshError(f"{path} missing payloads must set payload_complete false")
        if signal["source_timestamp"] is not None:
            _parse_timestamp(signal["source_timestamp"], f"{path}.source_timestamp")
        if signal["freshness_deadline"] is not None:
            _parse_timestamp(signal["freshness_deadline"], f"{path}.freshness_deadline")
        if type(signal["cadence_hours"]) not in {int, float} or signal["cadence_hours"] <= 0:
            raise EvidenceMeshError(f"{path}.cadence_hours is invalid")
        metric = signal["metric"]
        if metric is not None:
            metric = _require_exact(metric, _OSINT_METRIC_FIELDS, f"{path}.metric")
            if type(metric["value"]) not in {int, float} or isinstance(metric["value"], bool):
                raise EvidenceMeshError(f"{path}.metric.value is invalid")
            if metric["denominator"] is not None:
                denominator = _require_exact(
                    metric["denominator"], _OSINT_DENOMINATOR_FIELDS,
                    f"{path}.metric.denominator",
                )
                if type(denominator["value"]) not in {int, float} or isinstance(denominator["value"], bool):
                    raise EvidenceMeshError(f"{path}.metric.denominator.value is invalid")
    if value["n_signals_total"] != len(signals):
        raise EvidenceMeshError("OSINT signal count does not match signals")
    layers = value["layers"]
    if type(layers) is not list or len(layers) > 32:
        raise EvidenceMeshError("osint.layers must be a bounded array")
    referenced_signals: set[str] = set()
    for index, layer in enumerate(layers):
        layer = _require_exact(layer, _OSINT_LAYER_FIELDS, f"osint.layers[{index}]")
        for field in ("n_total", "n_reporting", "n_live", "n_degraded"):
            _safe_count(layer[field], f"osint.layers[{index}].{field}")
        if type(layer["signal_ids"]) is not list or len(layer["signal_ids"]) != len(set(layer["signal_ids"])):
            raise EvidenceMeshError("OSINT layer signal IDs must be unique")
        if not set(layer["signal_ids"]) <= ids:
            raise EvidenceMeshError("OSINT layer has a dangling signal")
        referenced_signals.update(layer["signal_ids"])
    if referenced_signals != ids:
        raise EvidenceMeshError("OSINT layers do not account for every signal")
    alerts = value["alerts"]
    if type(alerts) is not list or len(alerts) > 512:
        raise EvidenceMeshError("osint.alerts must be a bounded array")
    for index, alert in enumerate(alerts):
        alert = _require_exact(alert, _OSINT_ALERT_FIELDS, f"osint.alerts[{index}]")
        if alert["source_id"] not in ids:
            raise EvidenceMeshError("OSINT alert has a dangling signal")
    health = _require_exact(value["health"], _OSINT_TOP_HEALTH_FIELDS, "osint.health")
    counts = _require_exact(health["counts"], _OSINT_STATUS_COUNT_FIELDS, "osint.health.counts")
    for key, count in counts.items():
        _safe_count(count, f"osint.health.counts.{key}")
    return value


def _validate_manifest(value: Any) -> dict[str, Any]:
    value = _require_exact(value, _MANIFEST_TOP_FIELDS, "commons")
    if value["schema"] != "palimpsest-intelligence-commons-manifest/v1":
        raise EvidenceMeshError("unknown Intelligence Commons manifest schema")
    projects = value["projects"]
    if type(projects) is not list or not projects or len(projects) > MAX_PROJECTS:
        raise EvidenceMeshError("commons.projects must be bounded")
    project_ids: set[str] = set()
    for index, project in enumerate(projects):
        project = _require_exact(project, _MANIFEST_PROJECT_FIELDS, f"commons.projects[{index}]")
        project_id = _identifier(project["id"], f"commons.projects[{index}].id")
        if project_id in project_ids:
            raise EvidenceMeshError("duplicate Intelligence Commons project")
        project_ids.add(project_id)
        if project["status"] not in {"ACTIVE", "REVIEW_GATED", "PLANNED"}:
            raise EvidenceMeshError("unknown Intelligence Commons project status")
        public_url = _url(
            project["public_url"], f"commons.projects[{index}].public_url",
            nullable=True,
        )
        data_url = _url(
            project["data_url"], f"commons.projects[{index}].data_url",
            nullable=True,
        )
        if project["status"] == "ACTIVE" and (public_url is None or data_url is None):
            raise EvidenceMeshError("active Intelligence Commons projects need public URLs")
        if project_id in {"seiche", "liquilens"} and (
            project["status"] != "REVIEW_GATED"
            or public_url is not None
            or data_url is not None
        ):
            raise EvidenceMeshError(
                f"{project_id} must remain review-gated without a verified public URL"
            )
    for index, lane in enumerate(value["lanes"]):
        lane = _require_exact(lane, _MANIFEST_LANE_FIELDS, f"commons.lanes[{index}]")
        if not set(lane["project_ids"]) <= project_ids:
            raise EvidenceMeshError("Intelligence Commons lane has dangling project")
    for index, connection in enumerate(value["connections"]):
        connection = _require_exact(
            connection, _MANIFEST_CONNECTION_FIELDS, f"commons.connections[{index}]"
        )
        if connection["from_project_id"] not in project_ids or connection["to_project_id"] not in project_ids:
            raise EvidenceMeshError("Intelligence Commons connection has dangling project")
        data_url = _url(
            connection["data_url"], f"commons.connections[{index}].data_url",
            nullable=True,
        )
        if connection["status"] == "ACTIVE" and data_url is None:
            raise EvidenceMeshError("active Intelligence Commons connections need a data URL")
    if not {"palimpsest", "seiche", "liquilens", "scamshield", "narcoscope"} <= project_ids:
        raise EvidenceMeshError("Intelligence Commons is missing a required public project")
    return value


def _validate_scamshield_pack(value: Any) -> dict[str, Any]:
    value = _require_exact(value, _PACK_TOP_FIELDS, "scamshield_pack")
    if value["schema"] != "scamshield-intelligence-pack/v1":
        raise EvidenceMeshError("unknown ScamShield pack schema")
    _parse_timestamp(value["generated_at"], "scamshield_pack.generated_at")
    _require_exact(value["publisher"], frozenset({"name", "project_url"}), "scamshield_pack.publisher")
    _require_exact(value["method"], frozenset({"summary", "support_levels", "principles"}), "scamshield_pack.method")
    source_ids: set[str] = set()
    sources = value["sources"]
    if type(sources) is not list or not sources or len(sources) > 256:
        raise EvidenceMeshError("ScamShield sources must be bounded")
    for index, source in enumerate(sources):
        source = _require_exact(source, _PACK_SOURCE_FIELDS, f"scamshield_pack.sources[{index}]")
        source_id = _identifier(source["id"], f"scamshield_pack.sources[{index}].id")
        if source_id in source_ids:
            raise EvidenceMeshError("duplicate ScamShield source")
        source_ids.add(source_id)
        _url(source["url"], f"scamshield_pack.sources[{index}].url")
        _timestamp_or_date(source["published_at"], f"scamshield_pack.sources[{index}].published_at")
    typology_ids: set[str] = set()
    typologies = value["typologies"]
    if type(typologies) is not list or not typologies or len(typologies) > 128:
        raise EvidenceMeshError("ScamShield typologies must be bounded")
    for index, typology in enumerate(typologies):
        path = f"scamshield_pack.typologies[{index}]"
        typology = _require_exact(typology, _PACK_TYPOLOGY_FIELDS, path)
        typology_id = _identifier(typology["id"], f"{path}.id")
        if typology_id in typology_ids:
            raise EvidenceMeshError("duplicate ScamShield typology")
        typology_ids.add(typology_id)
        if not set(typology["source_refs"]) <= source_ids:
            raise EvidenceMeshError("ScamShield typology has dangling source reference")
        indicators = typology["indicators"]
        if type(indicators) is not list or len(indicators) > 64:
            raise EvidenceMeshError("ScamShield typology indicators must be bounded")
        for i_index, indicator in enumerate(indicators):
            _require_allowed(
                indicator, _PACK_INDICATOR_REQUIRED, _PACK_INDICATOR_ALLOWED,
                f"{path}.indicators[{i_index}]",
            )
    _scan_aggregate_only(value, "scamshield_pack")
    return value


def _load_partner_records(path: Path, maximum: int, project_id: str) -> tuple[list[dict[str, Any]], bytes]:
    value, raw = _load_json(path, maximum)
    if type(value) is dict:
        records = [value]
    elif type(value) is list and len(value) <= MAX_PARTNER_RECORDS:
        records = value
    else:
        raise EvidenceMeshError(f"{project_id} partner snapshot must be one envelope or a bounded array")
    if not records:
        raise EvidenceMeshError(f"{project_id} partner snapshot cannot be empty")
    # Reuse the normative Lab Evidence runtime, including digest verification,
    # timestamp/decimal rules, source-group checks, publication gates, and the
    # supersession graph.  The mesh then applies its narrower no-contact rule.
    try:
        validated = list(validate_envelope_set(records))
    except LabEvidenceError as exc:
        raise EvidenceMeshError(f"invalid {project_id} partner snapshot: {exc}") from exc
    for index, record in enumerate(validated):
        _scan_aggregate_only(record, f"{project_id}_snapshot[{index}]")
    return validated, raw


def _extract_clocks(payload: Mapping[str, Any]) -> dict[str, str | None]:
    def first(keys: Sequence[str]) -> datetime | None:
        for key in keys:
            value: Any = payload
            for part in key.split("."):
                if type(value) is not dict or part not in value:
                    value = None
                    break
                value = value[part]
            if value is not None:
                try:
                    return _timestamp_or_date(value, key)
                except EvidenceMeshError:
                    continue
        return None

    generated = first(("generated_at", "summary.generated_at", "ts"))
    event = first(("event_time", "data_as_of", "as_of")) or generated
    knowledge = first(("knowledge_time",)) or generated
    publication = first(("publication_time", "published_at")) or generated
    if event and knowledge and event > knowledge:
        event = knowledge
    if knowledge and publication and knowledge > publication:
        publication = knowledge
    return {
        "event_time": _iso_z(event) if event else None,
        "knowledge_time": _iso_z(knowledge) if knowledge else None,
        "publication_time": _iso_z(publication) if publication else None,
    }


def _rights(dataset: Mapping[str, Any]) -> dict[str, str]:
    if dataset.get("publication_allowed") is False:
        return {
            "redistribution": "RESTRICTED",
            "reuse": "metadata_only",
            "training": "prohibited",
        }
    name = str(dataset["license"]["name"]).casefold()
    status = dataset["status"]
    derived = dataset["collection_mode"].startswith("derived") or dataset["stage"] in {
        "synthesis", "quality", "publication", "integrity",
    }
    if status == "private-node" or dataset["collection_mode"] == "private-encrypted-editorial":
        return {"redistribution": "RESTRICTED", "reuse": "prohibited", "training": "prohibited"}
    if "mit for palimpsest output" in name:
        # Palimpsest's published aggregate fields are MIT; provider terms still
        # govern the raw probe responses, which are not copied into the mesh.
        redistribution = "ATTRIBUTION_REQUIRED"
        derived = True
    elif "upstream terms" in name or "provider terms" in name:
        redistribution = "LINK_ONLY"
    elif "cc0" in name or ("mit" in name and "upstream" not in name):
        redistribution = "OPEN"
    elif "cc by" in name or "attribution" in name or "upstream rights" in name:
        redistribution = "ATTRIBUTION_REQUIRED"
    else:
        redistribution = "UNKNOWN"
    reuse = "derived_only" if derived or redistribution in {"OPEN", "ATTRIBUTION_REQUIRED"} else "metadata_only"
    return {"redistribution": redistribution, "reuse": reuse, "training": "prohibited"}


def _catalog_role(dataset: Mapping[str, Any]) -> str:
    if dataset["stage"] == "reporting":
        return "candidate-only"
    if dataset["stage"] in {
        "planning", "synthesis", "quality", "source-metadata", "provenance", "publication",
    }:
        return "context"
    return "evidence"


def _catalog_class(dataset: Mapping[str, Any]) -> str:
    if dataset["stage"] == "integrity":
        return "INTEGRITY_PROOF"
    if dataset["stage"] in {"provenance", "publication", "reporting"}:
        return "PUBLICATION_WORKFLOW"
    if dataset["stage"] in {"planning", "source-metadata"}:
        return "METHOD_OR_ASSUMPTION"
    if dataset["stage"] in {"synthesis", "quality"} or dataset["collection_mode"].startswith("derived"):
        return "DERIVED_ANALYSIS"
    if dataset["id"] == "primary-documents":
        return "OFFICIAL_DOCUMENT"
    if dataset["layer"] == "economy":
        return "MARKET_OBSERVATION" if dataset["id"] in {"cny-fix-gap", "stock-connect"} else "OFFICIAL_STATISTIC"
    if dataset["layer"] == "model":
        return "METHOD_OR_ASSUMPTION"
    if dataset["id"] == "source-workflow":
        return "CONSENT_SCOPED_AGGREGATE"
    return "RESEARCH_DATASET"


def _available_from_osint(signal: Mapping[str, Any]) -> tuple[str, str]:
    status = signal["status"]
    if status == "live":
        return "available", "fresh"
    if status in {"stale", "degraded"}:
        return "stale", "stale"
    return "unavailable", "unavailable"


def _freshness(
    observed: datetime | None, deadline: datetime | None, now: datetime,
    cadence: str | None, availability: str, freshness_budget: str | None = None,
) -> dict[str, Any]:
    if availability == "unavailable":
        return {
            "status": "unavailable", "observed_at": None, "deadline": None,
            "age_hours": None, "cadence": cadence,
        }
    if observed is None:
        return {
            "status": "unknown", "observed_at": None, "deadline": None,
            "age_hours": None, "cadence": cadence,
        }
    # The public mesh contract serializes clocks at whole-second precision.
    # Normalize before doing freshness arithmetic so ``age_hours`` is derived
    # from the exact clocks a verifier will parse, including for sources such
    # as WDI whose acquisition receipts retain microseconds.
    observed = observed.astimezone(timezone.utc).replace(microsecond=0)
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    if deadline is not None:
        deadline = deadline.astimezone(timezone.utc).replace(microsecond=0)
    if observed > now:
        raise EvidenceMeshError("freshness observation cannot be after the build time")
    computed_deadline = deadline
    if computed_deadline is None and freshness_budget is not None:
        computed_deadline = observed + _duration(
            freshness_budget, "resource.freshness_budget"
        )
    elif computed_deadline is None and cadence is not None:
        computed_deadline = observed + 2 * _duration(cadence, "resource.cadence")
    state = "stale" if computed_deadline is not None and now > computed_deadline else "fresh"
    return {
        "status": state,
        "observed_at": _iso_z(observed),
        "deadline": _iso_z(computed_deadline) if computed_deadline else None,
        "age_hours": round((now - observed).total_seconds() / 3600.0, 3),
        "cadence": cadence,
    }


def _resource(
    *, resource_id: str, project_id: str, namespace: str, source_id: str, title: str,
    availability: str, allowed_role: str, evidence_class: str, independence_group: str,
    upstream_groups: Sequence[str], independence_eligible: bool,
    dependency_resource_ids: Sequence[str], rights: Mapping[str, str],
    clocks: Mapping[str, str | None], freshness: Mapping[str, Any], contract: str,
    public_url: str | None, input_id: str, limitations: Sequence[str],
    source_temporal_coverage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "resource_id": resource_id,
        "project_id": project_id,
        "namespace": namespace,
        "source_id": source_id,
        "title": title,
        "availability": availability,
        "allowed_role": allowed_role,
        "evidence_class": evidence_class,
        "independence_group": independence_group,
        "upstream_groups": sorted(set(upstream_groups)),
        "independence_eligible": independence_eligible,
        "dependency_resource_ids": sorted(set(dependency_resource_ids)),
        "rights": dict(rights),
        "clocks": dict(clocks),
        "freshness": dict(freshness),
        "source_temporal_coverage": (
            dict(source_temporal_coverage)
            if source_temporal_coverage is not None else None
        ),
        "contract": contract,
        "public_url": public_url,
        "input_id": input_id,
        "limitations": list(limitations),
    }


def _receipt(
    *, input_id: str, project_id: str, contract: str, required: bool,
    availability: str, locator: str, public_url: str | None, raw: bytes | None,
    observed_version: str | None, observed_at: str | None,
    expected_sha256: str | None = None, resource_count: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    digest = _sha(raw) if raw is not None else None
    if expected_sha256 is None:
        byte_identity = "not-checked"
    elif digest == expected_sha256:
        byte_identity = "match"
    else:
        byte_identity = "mismatch"
    return {
        "input_id": input_id,
        "project_id": project_id,
        "contract": contract,
        "required": required,
        "availability": availability,
        "locator": locator,
        "public_url": public_url,
        "sha256": digest,
        "bytes": len(raw) if raw is not None else None,
        "observed_version": observed_version,
        "observed_at": observed_at,
        "expected_sha256": expected_sha256,
        "byte_identity": byte_identity,
        "resource_count": resource_count,
        "reason": reason,
    }


def build_evidence_mesh(
    root: Path | str = ROOT,
    *,
    now: datetime | None = None,
    config_path: Path | str | None = None,
    partner_snapshot_paths: Mapping[str, Path | str] | None = None,
) -> dict[str, Any]:
    """Build one deterministic evidence mesh without network access.

    ``partner_snapshot_paths`` accepts ``seiche`` and ``liquilens`` keys (their
    input-contract IDs are accepted as aliases).  Missing optional files are
    inventory states, not errors.  A supplied but invalid file fails closed.
    """

    root = Path(root).resolve()
    supplied_now = now or datetime.now(timezone.utc)
    if supplied_now.tzinfo is None or supplied_now.utcoffset() is None:
        raise EvidenceMeshError("build time must be timezone-aware")
    moment = supplied_now.astimezone(timezone.utc).replace(microsecond=0)
    configured_path = Path(config_path) if config_path is not None else root / "config" / "evidence_mesh.json"
    config_value, _ = _load_json(configured_path, MAX_CONFIG_BYTES)
    config = _validate_config(config_value)
    maximum = config["max_input_bytes"]
    projects_config = {project["id"]: project for project in config["projects"]}
    contracts = {
        contract["id"]: (project["id"], contract)
        for project in config["projects"] for contract in project["input_contracts"]
    }

    supplied = dict(partner_snapshot_paths or {})
    aliases = {
        "seiche": "seiche-partner-snapshot",
        "liquilens": "liquilens-partner-snapshot",
    }
    normalized_supplied: dict[str, Path] = {}
    for key, value in supplied.items():
        input_id = aliases.get(key, key)
        if input_id not in {"seiche-partner-snapshot", "liquilens-partner-snapshot"}:
            raise EvidenceMeshError(f"unknown partner snapshot key: {key}")
        if input_id in normalized_supplied:
            raise EvidenceMeshError(f"duplicate partner snapshot alias: {key}")
        normalized_supplied[input_id] = Path(value)

    def load_contract(input_id: str) -> tuple[Any, bytes, Mapping[str, Any]]:
        _, contract = contracts[input_id]
        path = _repo_path(root, contract["local_path"], f"contract.{input_id}.local_path")
        value, raw = _load_json(path, maximum)
        return value, raw, contract

    catalog_value, catalog_raw, catalog_contract = load_contract("palimpsest-catalog")
    catalog = _validate_catalog(catalog_value)
    osint_value, osint_raw, osint_contract = load_contract("palimpsest-osint")
    osint = _validate_osint(osint_value)
    commons_value, commons_raw, commons_contract = load_contract("intelligence-commons")
    commons = _validate_manifest(commons_value)
    pack_value, pack_raw, pack_contract = load_contract("scamshield-typology-pack")
    pack = _validate_scamshield_pack(pack_value)
    narco_value, narco_raw, narco_contract = load_contract("narcoscope-china-aggregate")
    if type(narco_value) is not dict:
        raise EvidenceMeshError("NarcoScope artifact must be an object")
    try:
        validate_narcoscope_artifact(narco_value)
    except NarcoScopeBridgeError as exc:
        raise EvidenceMeshError(f"invalid NarcoScope artifact: {exc}") from exc
    narco = narco_value
    pin_value, pin_raw, pin_contract = load_contract("narcoscope-pin-receipt")
    if type(pin_value) is not dict:
        raise EvidenceMeshError("NarcoScope pin receipt must be an object")
    try:
        validate_narcoscope_receipt(pin_value)
    except NarcoScopeBridgeError as exc:
        raise EvidenceMeshError(f"invalid NarcoScope pin receipt: {exc}") from exc
    pin_receipt = pin_value
    expected_narco = pin_receipt["current"]
    narco_observed = _parse_timestamp(
        expected_narco["admitted_at"], "narcoscope.pin.current.admitted_at"
    )
    if narco_observed > moment:
        raise EvidenceMeshError("NarcoScope pin admission is after the build time")

    catalog_rows = {row["id"]: row for row in catalog["datasets"]}
    osint_rows = {row["id"]: row for row in osint["signals"]}
    dependencies: dict[str, list[str]] = config["derived_dependencies"]

    direct_groups: dict[str, list[str]] = {}
    for dataset_id, dataset in catalog_rows.items():
        direct_groups[dataset_id] = [f"publisher:{_slug(source)}" for source in dataset["sources"]]
    direct_groups.setdefault("nemesis", ["pipeline:optional-nemesis"])

    group_cache: dict[str, list[str]] = {}

    def upstream_groups(dataset_id: str, trail: tuple[str, ...] = ()) -> list[str]:
        if dataset_id in group_cache:
            return group_cache[dataset_id]
        if dataset_id in trail:
            raise EvidenceMeshError(f"derived dependency cycle: {' -> '.join((*trail, dataset_id))}")
        if dataset_id in dependencies:
            groups = sorted({
                group for dependency in dependencies[dataset_id]
                for group in upstream_groups(dependency, (*trail, dataset_id))
            })
        else:
            groups = direct_groups.get(dataset_id, [f"pipeline:palimpsest-{dataset_id}"])
        group_cache[dataset_id] = groups
        return groups

    resources: list[dict[str, Any]] = []
    bri_observation_inputs: list[dict[str, Any]] = []
    catalog_publication = _parse_timestamp(osint["generated_at"], "osint.generated_at")
    for dataset in catalog["datasets"]:
        dataset_id = dataset["id"]
        signal = osint_rows.get(dataset_id)
        latest_path = _repo_path(root, dataset["latest"], f"catalog.{dataset_id}.latest")
        publication_plane = dataset_id in PUBLICATION_PLANE_IDS
        payload: Mapping[str, Any] | None = None
        if latest_path.exists() and not publication_plane:
            try:
                loaded, _raw = _load_json(latest_path, maximum)
                if type(loaded) is dict:
                    payload = loaded
            except EvidenceMeshError:
                payload = None
        if signal is not None:
            availability, freshness_state = _available_from_osint(signal)
            observed = (
                _parse_timestamp(signal["source_timestamp"], f"signal.{dataset_id}.source_timestamp")
                if signal["source_timestamp"] is not None else None
            )
            deadline = (
                _parse_timestamp(signal["freshness_deadline"], f"signal.{dataset_id}.freshness_deadline")
                if signal["freshness_deadline"] is not None else None
            )
            clocks = {
                "event_time": _iso_z(observed) if observed else None,
                "knowledge_time": _iso_z(observed) if observed else None,
                "publication_time": _iso_z(catalog_publication),
            }
            fresh = _freshness(
                observed, deadline, moment, dataset["cadence"], availability,
                dataset.get("freshness_budget"),
            )
            fresh["status"] = freshness_state
        else:
            if publication_plane:
                # These resources are downstream discovery surfaces.  Their
                # catalog declaration is stable input, but their file
                # presence is not: the files are created only after this mesh
                # is sealed.  Treat a declared live plane as available without
                # observing its bytes, or the graph changes merely because a
                # later build step completed.
                availability = (
                    "unavailable" if dataset["status"] == "disabled" else "available"
                )
            elif dataset["status"] == "disabled" or not latest_path.exists():
                availability = "unavailable"
            else:
                availability = "available"
            # Publication planes are catalogued for discovery but never loaded
            # into the mesh that precedes them in the build graph.  Reading
            # their clocks or bytes here would create a recursive hash chain:
            # mesh -> machine publication -> mesh.  File presence is enough to
            # report availability without manufacturing evidence lineage.
            clocks = (
                {"event_time": None, "knowledge_time": None, "publication_time": None}
                if publication_plane else _extract_clocks(payload or {})
            )
            observed_value = clocks["knowledge_time"] or clocks["publication_time"] or clocks["event_time"]
            observed = _parse_timestamp(observed_value, f"catalog.{dataset_id}.clock") if observed_value else None
            fresh = _freshness(
                observed, None, moment, dataset["cadence"], availability,
                dataset.get("freshness_budget"),
            )
            if fresh["status"] == "stale":
                availability = "stale"
        if dataset.get("publication_allowed") is False:
            availability = "unavailable"
            fresh = _freshness(
                None,
                None,
                moment,
                dataset["cadence"],
                availability,
                dataset.get("freshness_budget"),
            )
        role = _catalog_role(dataset)
        evidence_class = _catalog_class(dataset)
        groups = upstream_groups(dataset_id)
        derived = dataset_id in dependencies or evidence_class in {
            "DERIVED_ANALYSIS", "PUBLICATION_WORKFLOW", "METHOD_OR_ASSUMPTION",
        }
        independence_group = (
            f"derived:palimpsest-{dataset_id}" if derived
            else groups[0] if len(groups) == 1
            else f"pipeline:palimpsest-{dataset_id}"
        )
        dependency_ids = [
            f"palimpsest:catalog:{item}" for item in dependencies.get(dataset_id, [])
            if item in catalog_rows
        ]
        public_url = "https://palimpsest.info/" + dataset["latest"]
        limitations = [dataset["description"]]
        if dataset.get("freshness_semantics") is not None:
            limitations.append(dataset["freshness_semantics"])
        if dataset.get("publication_allowed") is False:
            limitations.append(
                "Current source policy restricts value publication; this resource is metadata-only."
            )
        if availability != "available":
            limitations.append("The current resource is unavailable or stale; absence is not a zero observation.")
        resources.append(_resource(
            resource_id=f"palimpsest:catalog:{dataset_id}", project_id="palimpsest",
            namespace="catalog", source_id=dataset_id, title=dataset["name"],
            availability=availability, allowed_role=role, evidence_class=evidence_class,
            independence_group=independence_group, upstream_groups=groups,
            independence_eligible=(role == "evidence" and not derived),
            dependency_resource_ids=dependency_ids, rights=_rights(dataset), clocks=clocks,
            freshness=fresh, contract="palimpsest-public-data-catalog/v1",
            public_url=public_url, input_id="palimpsest-catalog", limitations=limitations,
        ))
        if (
            dataset_id == "belt-and-road-observatory"
            and payload is not None
            and payload.get("schema_version")
            == "palimpsest.belt-and-road-observatory.v2"
        ):
            descriptors = payload.get("observation_datasets")
            if type(descriptors) is not list or len(descriptors) != 1:
                raise EvidenceMeshError(
                    "BRI observatory v2 must expose exactly one observation dataset"
                )
            descriptor = descriptors[0]
            if not isinstance(descriptor, Mapping):
                raise EvidenceMeshError("BRI observation descriptor must be an object")
            try:
                artifact_path = _repo_path(
                    root,
                    descriptor["artifact"]["path"],
                    "bri.observation.artifact.path",
                )
                artifact_document, artifact_raw = _load_json(artifact_path, maximum)
                observation_schema_path = _repo_path(
                    root,
                    descriptor["observation_schema"]["path"],
                    "bri.observation.schema.path",
                )
                _observation_schema, observation_schema_raw = _load_json(
                    observation_schema_path,
                    maximum,
                )
                series_registry_path = _repo_path(
                    root,
                    descriptor["series_registry"]["path"],
                    "bri.observation.series_registry.path",
                )
                _series_registry, series_registry_raw = _load_json(
                    series_registry_path,
                    maximum,
                )
                bri_registry = registry_projection_from_public_artifact(payload)
                validated_descriptor = validate_observation_dataset_descriptor(
                    descriptor,
                    registry=bri_registry,
                    artifact_raw=artifact_raw,
                    artifact_document=artifact_document,
                    observation_schema_raw=observation_schema_raw,
                    series_registry_raw=series_registry_raw,
                    series_registry_path=series_registry_path,
                )
            except (BriRegistryError, KeyError, TypeError) as exc:
                raise EvidenceMeshError(
                    f"invalid BRI WDI observation descriptor: {exc}"
                ) from exc
            clocks = validated_descriptor["clocks"]
            retrieved_at = _parse_timestamp(
                clocks["retrieved_at"],
                "bri.observation.clocks.retrieved_at",
            )
            coverage = validated_descriptor["coverage"]
            input_id = "palimpsest-bri-wdi-world-bank"
            resources.append(_resource(
                resource_id="palimpsest:context:bri-world-bank-wdi",
                project_id="palimpsest",
                namespace="context",
                source_id="world-bank-wdi",
                title="World Bank WDI national economic context for BRI countries",
                availability="available",
                allowed_role="context",
                evidence_class="OFFICIAL_STATISTIC",
                independence_group="publisher:world-bank",
                upstream_groups=["publisher:world-bank"],
                independence_eligible=False,
                dependency_resource_ids=[
                    "palimpsest:catalog:belt-and-road-observatory"
                ],
                rights={
                    "redistribution": "ATTRIBUTION_REQUIRED",
                    "reuse": "full_text",
                    "training": "prohibited",
                },
                clocks={
                    "event_time": None,
                    "knowledge_time": _iso_z(retrieved_at),
                    "publication_time": None,
                },
                freshness=_freshness(
                    retrieved_at,
                    None,
                    moment,
                    "P1Y",
                    "available",
                ),
                source_temporal_coverage={
                    "kind": "year_range",
                    "from_year": coverage["start_year"],
                    "to_year": coverage["end_year"],
                    "snapshot_date": None,
                },
                contract="palimpsest.bri-economic-observations.v1",
                public_url=validated_descriptor["artifact"]["url"],
                input_id=input_id,
                limitations=[
                    "Country-period context only; this resource cannot establish a BRI project, corridor, actor or causal effect.",
                    "Source-marked forecasts remain forecasts and source-null values remain unavailable, never observed zeroes.",
                    "The null publication receipt means repository-ready bytes are not yet production-verified.",
                ],
            ))
            bri_observation_inputs.append(_receipt(
                input_id=input_id,
                project_id="palimpsest",
                contract="palimpsest.bri-economic-observations.v1",
                required=False,
                availability="available",
                locator=validated_descriptor["artifact"]["path"],
                public_url=validated_descriptor["artifact"]["url"],
                raw=artifact_raw,
                observed_version=artifact_document["schema_version"],
                observed_at=validated_descriptor["generated_at"],
                expected_sha256=validated_descriptor["artifact"]["sha256"],
                resource_count=coverage["source_rows"],
                reason=(
                    "Repository-ready context bytes; publication receipt remains null "
                    "until an exact production deployment is verified."
                ),
            ))

    for signal in osint["signals"]:
        signal_id = signal["id"]
        dataset = catalog_rows.get(signal_id)
        if dataset is not None:
            role = _catalog_role(dataset)
            evidence_class = _catalog_class(dataset)
            rights = _rights(dataset)
        else:
            role = "candidate-only"
            evidence_class = "CONSENT_SCOPED_AGGREGATE"
            rights = {"redistribution": "RESTRICTED", "reuse": "metadata_only", "training": "prohibited"}
        availability, freshness_state = _available_from_osint(signal)
        observed = (
            _parse_timestamp(signal["source_timestamp"], f"signal.{signal_id}.source_timestamp")
            if signal["source_timestamp"] is not None else None
        )
        deadline = (
            _parse_timestamp(signal["freshness_deadline"], f"signal.{signal_id}.freshness_deadline")
            if signal["freshness_deadline"] is not None else None
        )
        fresh = _freshness(observed, deadline, moment, f"PT{signal['cadence_hours']}H", availability)
        fresh["status"] = freshness_state
        groups = upstream_groups(signal_id)
        derived = signal_id in dependencies or evidence_class in {
            "DERIVED_ANALYSIS", "PUBLICATION_WORKFLOW", "METHOD_OR_ASSUMPTION",
        }
        independence_group = (
            f"derived:palimpsest-{signal_id}" if derived
            else groups[0] if len(groups) == 1
            else f"pipeline:palimpsest-{signal_id}"
        )
        dependency_ids = [
            (f"palimpsest:osint:{item}" if item in osint_rows else f"palimpsest:catalog:{item}")
            for item in dependencies.get(signal_id, []) if item in osint_rows or item in catalog_rows
        ]
        limitations = [signal["scope"]]
        if availability != "available":
            limitations.append("This signal is unavailable or stale; no zero-valued observation is inferred.")
        resources.append(_resource(
            resource_id=f"palimpsest:osint:{signal_id}", project_id="palimpsest",
            namespace="osint", source_id=signal_id, title=signal["title"],
            availability=availability, allowed_role=role, evidence_class=evidence_class,
            independence_group=independence_group, upstream_groups=groups,
            independence_eligible=(role == "evidence" and not derived),
            dependency_resource_ids=dependency_ids, rights=rights,
            clocks={
                "event_time": _iso_z(observed) if observed else None,
                "knowledge_time": _iso_z(observed) if observed else None,
                "publication_time": _iso_z(catalog_publication),
            }, freshness=fresh, contract="osint-china.v1", public_url=signal["raw_url"],
            input_id="palimpsest-osint", limitations=limitations,
        ))

    pin = config["narcoscope_pin"]
    try:
        validate_narcoscope_receipt(pin_receipt, artifact=narco_raw)
        narco_matches = True
    except NarcoScopeBridgeError:
        # Artifact and receipt were each validated independently above.  A
        # binding mismatch therefore remains an explicit stale-pin state rather
        # than being laundered into current evidence.
        narco_matches = False
    narco_deadline = narco_observed + timedelta(days=pin["freshness_days"])
    narco_availability = "available" if narco_matches and moment <= narco_deadline else "stale"
    narco_freshness = _freshness(
        narco_observed, narco_deadline, moment, f"P{pin['freshness_days']}D",
        narco_availability,
    )
    if narco_availability == "stale":
        narco_freshness["status"] = "stale"
    source_by_id = {row["id"]: row for row in pack["sources"]}
    for key, dataset in narco["datasets"].items():
        source_id = dataset["datasetId"].replace("_", "-")
        publisher_group = f"publisher:{_slug(dataset['provenance']['publisher'])}"
        evidence_class = (
            "OFFICIAL_DOCUMENT" if dataset["measurement"]["valueType"] == "administrative_action"
            else "OFFICIAL_STATISTIC"
        )
        coverage = dataset["temporalCoverage"]
        source_temporal_coverage = {
            "kind": coverage["kind"],
            "from_year": coverage["fromYear"],
            "to_year": coverage["toYear"],
            "snapshot_date": coverage["snapshotDate"],
        }
        if coverage["kind"] == "year_range":
            event_time = datetime(
                coverage["toYear"], 12, 31, 23, 59, 59, tzinfo=timezone.utc
            )
        else:
            # A date-only snapshot describes a civil-day interval, not a known
            # instant. Preserve it in source_temporal_coverage instead of
            # inventing midnight or end-of-day precision.
            event_time = None
        if event_time is not None and event_time > narco_observed:
            raise EvidenceMeshError(
                f"NarcoScope {key} source coverage ends after its admission clock"
            )
        resources.append(_resource(
            resource_id=f"narcoscope:artifact:{source_id}", project_id="narcoscope",
            namespace="artifact", source_id=source_id, title=dataset["topic"].replace("_", " ").title(),
            availability=narco_availability, allowed_role="evidence",
            evidence_class=evidence_class, independence_group=publisher_group,
            upstream_groups=[publisher_group], independence_eligible=True,
            dependency_resource_ids=[], rights={
                "redistribution": "ATTRIBUTION_REQUIRED", "reuse": "derived_only",
                "training": "prohibited",
            }, clocks={
                "event_time": _iso_z(event_time) if event_time else None,
                "knowledge_time": _iso_z(narco_observed),
                "publication_time": _iso_z(narco_observed),
            }, freshness=narco_freshness,
            source_temporal_coverage=source_temporal_coverage,
            contract="narcoscope.palimpsest.china-aggregate.v1",
            public_url=narco_contract["public_url"], input_id="narcoscope-china-aggregate",
            limitations=dataset["limitations"],
        ))

    pack_generated = _parse_timestamp(pack["generated_at"], "scamshield_pack.generated_at")
    for typology in pack["typologies"]:
        upstream = sorted({
            f"publisher:{_slug(source_by_id[source_id]['publisher'])}"
            for source_id in typology["source_refs"]
        })
        resources.append(_resource(
            resource_id=f"scamshield:typology:{typology['id']}", project_id="scamshield",
            namespace="typology", source_id=typology["id"], title=typology["label"],
            availability="available", allowed_role="typology",
            evidence_class="REVIEWED_REPORT",
            independence_group=f"typology:scamshield-{typology['id']}",
            upstream_groups=upstream, independence_eligible=False,
            dependency_resource_ids=[], rights={
                "redistribution": "ATTRIBUTION_REQUIRED", "reuse": "metadata_only",
                "training": "prohibited",
            }, clocks={
                "event_time": None, "knowledge_time": _iso_z(pack_generated),
                "publication_time": _iso_z(pack_generated),
            }, freshness=_freshness(pack_generated, None, moment, None, "available"),
            contract="scamshield-intelligence-pack/v1", public_url=pack_contract["public_url"],
            input_id="scamshield-typology-pack", limitations=typology["limitations"],
        ))

    inputs: list[dict[str, Any]] = [
        _receipt(
            input_id="palimpsest-catalog", project_id="palimpsest",
            contract=catalog_contract["contract"], required=True, availability="available",
            locator=catalog_contract["local_path"], public_url=catalog_contract["public_url"],
            raw=catalog_raw, observed_version=catalog["schema_version"], observed_at=None,
            resource_count=len(catalog["datasets"]),
        ),
        _receipt(
            input_id="palimpsest-osint", project_id="palimpsest",
            contract=osint_contract["contract"], required=True, availability="available",
            locator=osint_contract["local_path"], public_url=osint_contract["public_url"],
            raw=osint_raw, observed_version=osint["schema_version"],
            observed_at=_iso_z(_parse_timestamp(osint["generated_at"], "osint.generated_at")),
            resource_count=len(osint["signals"]),
        ),
        _receipt(
            input_id="intelligence-commons", project_id="palimpsest",
            contract=commons_contract["contract"], required=True, availability="available",
            locator=commons_contract["local_path"], public_url=commons_contract["public_url"],
            raw=commons_raw, observed_version=commons["version"], observed_at=None,
            resource_count=len(commons["projects"]),
        ),
        _receipt(
            input_id="scamshield-typology-pack", project_id="scamshield",
            contract=pack_contract["contract"], required=True, availability="available",
            locator=pack_contract["local_path"], public_url=pack_contract["public_url"],
            raw=pack_raw, observed_version=pack["version"], observed_at=_iso_z(pack_generated),
            resource_count=len(pack["typologies"]),
        ),
        _receipt(
            input_id="narcoscope-china-aggregate", project_id="narcoscope",
            contract=narco_contract["contract"], required=True, availability=narco_availability,
            locator=narco_contract["local_path"], public_url=narco_contract["public_url"],
            raw=narco_raw, observed_version=narco["schemaVersion"],
            observed_at=_iso_z(narco_observed), expected_sha256=expected_narco["sha256"],
            resource_count=len(narco["datasets"]),
            reason=(
                None if narco_matches else
                "Pinned artifact differs from the declared current producer date or byte hash; retained as stale context."
            ),
        ),
        _receipt(
            input_id="narcoscope-pin-receipt", project_id="narcoscope",
            contract=pin_contract["contract"], required=True, availability="available",
            locator=pin_contract["local_path"], public_url=pin_contract["public_url"],
            raw=pin_raw, observed_version=pin_receipt["schema"],
            observed_at=pin_receipt["current"]["admitted_at"],
            resource_count=len(pin_receipt["superseded"]) + 1,
        ),
    ]
    inputs.extend(bri_observation_inputs)

    for project_id, input_id in (("seiche", "seiche-partner-snapshot"), ("liquilens", "liquilens-partner-snapshot")):
        contract = contracts[input_id][1]
        path = normalized_supplied.get(input_id)
        if path is None or not path.exists():
            inputs.append(_receipt(
                input_id=input_id, project_id=project_id, contract=contract["contract"],
                required=False, availability="unavailable", locator=f"caller:{input_id}",
                public_url=contract["public_url"], raw=None, observed_version=None,
                observed_at=None, resource_count=None,
                reason="No optional aggregate partner snapshot was supplied; no observation is inferred.",
            ))
            resources.append(_resource(
                resource_id=f"{project_id}:partner:unavailable", project_id=project_id,
                namespace="partner", source_id=f"{project_id}-partner-snapshot",
                title=f"{projects_config[project_id]['title']} aggregate partner snapshot",
                availability="unavailable", allowed_role="context",
                evidence_class="METHOD_OR_ASSUMPTION",
                independence_group=f"pipeline:{project_id}-partner",
                upstream_groups=[f"pipeline:{project_id}-partner"],
                independence_eligible=False, dependency_resource_ids=[], rights={
                    "redistribution": "UNKNOWN", "reuse": "prohibited", "training": "prohibited",
                }, clocks={"event_time": None, "knowledge_time": None, "publication_time": None},
                freshness=_freshness(None, None, moment, None, "unavailable"),
                contract="lab-evidence-envelope/v1", public_url=None, input_id=input_id,
                limitations=["Optional snapshot unavailable; this placeholder carries no measurement and cannot corroborate a claim."],
            ))
            continue
        records, raw = _load_partner_records(path, maximum, project_id)
        latest_publication = max(_parse_timestamp(row["publication_time"], "publication_time") for row in records)
        inputs.append(_receipt(
            input_id=input_id, project_id=project_id, contract=contract["contract"],
            required=False, availability="available", locator=f"caller:{input_id}",
            public_url=contract["public_url"], raw=raw,
            observed_version="lab-evidence-envelope/v1", observed_at=_iso_z(latest_publication),
            resource_count=len(records),
        ))
        for record in records:
            source_groups = sorted(f"partner:{project_id}:{_slug(group)}" for group in record["source_groups"])
            allowed_role = contract["allowed_role"]
            evidence_class = record["source_refs"][0]["evidence_class"]
            if evidence_class not in EVIDENCE_CLASSES:
                evidence_class = "RESEARCH_DATASET"
            event = _parse_timestamp(record["event_time"], "record.event_time")
            publication = _parse_timestamp(record["publication_time"], "record.publication_time")
            resources.append(_resource(
                resource_id=f"{project_id}:partner:{record['record_id']}", project_id=project_id,
                namespace="partner", source_id=record["signal_id"], title=record["signal_id"].replace("-", " ").title(),
                availability="available", allowed_role=allowed_role,
                evidence_class=evidence_class,
                independence_group=(source_groups[0] if len(source_groups) == 1 else f"pipeline:{project_id}-{record['signal_id']}"),
                upstream_groups=source_groups, independence_eligible=(allowed_role == "evidence"),
                dependency_resource_ids=[], rights={
                    "redistribution": record["redistribution_status"],
                    "reuse": "derived_only" if record["public_value_allowed"] else "metadata_only",
                    "training": "prohibited",
                }, clocks={
                    "event_time": _iso_z(event),
                    "knowledge_time": _iso_z(_parse_timestamp(record["knowledge_time"], "record.knowledge_time")),
                    "publication_time": _iso_z(publication),
                }, freshness=_freshness(event, None, moment, None, "available"),
                contract="lab-evidence-envelope/v1", public_url=None, input_id=input_id,
                limitations=record["limitations"],
            ))

    resources.sort(key=lambda item: item["resource_id"])
    if len(resources) > MAX_RESOURCES:
        raise EvidenceMeshError("evidence mesh exceeds its resource bound")
    resource_ids = {row["resource_id"] for row in resources}
    for row in resources:
        dangling = set(row["dependency_resource_ids"]) - resource_ids
        if dangling:
            raise EvidenceMeshError(f"resource {row['resource_id']} has dangling dependencies: {sorted(dangling)}")

    manifest_ids = {row["id"] for row in commons["projects"]}
    projects: list[dict[str, Any]] = []
    for config_project in config["projects"]:
        project_id = config_project["id"]
        project_resources = [row["resource_id"] for row in resources if row["project_id"] == project_id]
        states = {row["availability"] for row in resources if row["project_id"] == project_id}
        if "available" in states:
            availability = "available"
        elif "stale" in states:
            availability = "stale"
        else:
            availability = "unavailable"
        projects.append({
            "id": project_id,
            "title": config_project["title"],
            "status": config_project["status"],
            "public_url": config_project["public_url"],
            "manifest_status": "declared" if project_id in manifest_ids else "not-declared",
            "availability": availability,
            "capabilities": list(config_project["capabilities"]),
            "input_contracts": [dict(value) for value in config_project["input_contracts"]],
            "resource_ids": project_resources,
        })

    dependency_groups: list[dict[str, Any]] = []
    all_groups = sorted({group for row in resources for group in row["upstream_groups"]})
    for group_id in all_groups:
        members = [row for row in resources if group_id in row["upstream_groups"]]
        dependency_groups.append({
            "group_id": group_id,
            "resource_ids": [row["resource_id"] for row in members],
            "available_resource_count": sum(row["availability"] == "available" for row in members),
            "independence_eligible_resource_count": sum(
                row["availability"] == "available" and row["independence_eligible"] for row in members
            ),
        })

    role_counts = [
        {"id": role, "count": sum(row["allowed_role"] == role for row in resources)}
        for role in sorted(ALLOWED_ROLES)
    ]
    project_counts = [
        {"id": project_id, "count": sum(row["project_id"] == project_id for row in resources)}
        for project_id in PROJECT_IDS
    ]
    independent_available = {
        row["independence_group"] for row in resources
        if row["availability"] == "available" and row["independence_eligible"]
    }
    optional_unavailable = sum(
        not receipt["required"] and receipt["availability"] == "unavailable" for receipt in inputs
    )
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_z(moment),
        "source": "Checked-in Palimpsest catalog, OSINT surface, Intelligence Commons, and bounded aggregate partner artifacts",
        "method": "Deterministic no-network inventory with strict contracts, source-lineage de-duplication, rights gates, clocks, freshness, and explicit unavailable states.",
        "scope": "Aggregate evidence and analytical context only; the mesh does not identify people, expose exact indicators, infer guilt, or convert a mirror or derived view into independent corroboration.",
        "safety": {
            "aggregate_only": True,
            "person_level_data": False,
            "contact_data": False,
            "exact_iocs": False,
            "missing_is_zero": False,
            "automatic_attribution": False,
        },
        "inputs": sorted(inputs, key=lambda item: item["input_id"]),
        "projects": projects,
        "resources": resources,
        "dependency_groups": dependency_groups,
        "summary": {
            "project_count": len(projects),
            "resource_count": len(resources),
            "available_resource_count": sum(row["availability"] == "available" for row in resources),
            "stale_resource_count": sum(row["availability"] == "stale" for row in resources),
            "unavailable_resource_count": sum(row["availability"] == "unavailable" for row in resources),
            "independent_groups_available": len(independent_available),
            "optional_inputs_unavailable": optional_unavailable,
            "palimpsest_catalog": {
                "expected": len(catalog["datasets"]), "accounted": sum(
                    row["project_id"] == "palimpsest" and row["namespace"] == "catalog" for row in resources
                ), "complete": True,
            },
            "palimpsest_osint": {
                "expected": len(osint["signals"]), "accounted": sum(
                    row["project_id"] == "palimpsest" and row["namespace"] == "osint" for row in resources
                ), "complete": True,
            },
            "project_resource_counts": project_counts,
            "role_counts": role_counts,
        },
    }
    validate_evidence_mesh(document)
    return document


def validate_evidence_mesh(document: Any) -> None:
    """Validate structural, referential, safety, and summary invariants."""

    document = _require_exact(document, _TOP_FIELDS, "mesh")
    if document["schema_version"] != SCHEMA_VERSION:
        raise EvidenceMeshError("unknown evidence-mesh schema")
    generated_at = _parse_timestamp(document["generated_at"], "mesh.generated_at")
    safety = _require_exact(document["safety"], _SAFETY_FIELDS, "mesh.safety")
    if safety != {
        "aggregate_only": True, "person_level_data": False, "contact_data": False,
        "exact_iocs": False, "missing_is_zero": False, "automatic_attribution": False,
    }:
        raise EvidenceMeshError("mesh safety boundary is not fail-closed")

    inputs = document["inputs"]
    if type(inputs) is not list or len(inputs) > 64:
        raise EvidenceMeshError("mesh.inputs must be bounded")
    input_ids: set[str] = set()
    for index, receipt in enumerate(inputs):
        receipt = _require_exact(receipt, _INPUT_RECEIPT_FIELDS, f"mesh.inputs[{index}]")
        input_id = _identifier(receipt["input_id"], f"mesh.inputs[{index}].input_id")
        if input_id in input_ids:
            raise EvidenceMeshError("duplicate mesh input receipt")
        input_ids.add(input_id)
        if receipt["availability"] not in AVAILABILITY:
            raise EvidenceMeshError("unknown input availability")
        if receipt["project_id"] not in PROJECT_IDS:
            raise EvidenceMeshError("input receipt references an unknown project")
        _url(receipt["public_url"], f"mesh.inputs[{index}].public_url", nullable=True)
        if receipt["availability"] == "unavailable":
            if any(receipt[key] is not None for key in ("sha256", "bytes", "observed_version", "observed_at", "resource_count")):
                raise EvidenceMeshError("unavailable input must retain null observations, never zero")
        if receipt["sha256"] is not None and not _SHA_RE.fullmatch(receipt["sha256"]):
            raise EvidenceMeshError("invalid input SHA-256")
        if receipt["expected_sha256"] is not None and not _SHA_RE.fullmatch(receipt["expected_sha256"]):
            raise EvidenceMeshError("invalid expected input SHA-256")
        if receipt["byte_identity"] not in {"match", "mismatch", "not-checked"}:
            raise EvidenceMeshError("unknown byte identity status")

    projects = document["projects"]
    if type(projects) is not list or len(projects) > MAX_PROJECTS:
        raise EvidenceMeshError("mesh.projects must be bounded")
    project_ids: list[str] = []
    for index, project in enumerate(projects):
        project = _require_exact(project, _PROJECT_FIELDS, f"mesh.projects[{index}]")
        project_ids.append(_identifier(project["id"], f"mesh.projects[{index}].id"))
        if project["availability"] not in AVAILABILITY:
            raise EvidenceMeshError("unknown project availability")
        _url(project["public_url"], f"mesh.projects[{index}].public_url", nullable=True)
        for c_index, contract in enumerate(project["input_contracts"]):
            _require_exact(contract, _INPUT_CONTRACT_FIELDS, f"mesh.projects[{index}].input_contracts[{c_index}]")
    if project_ids != list(PROJECT_IDS):
        raise EvidenceMeshError("mesh must enumerate the five projects in stable order")

    resources = document["resources"]
    if type(resources) is not list or not resources or len(resources) > MAX_RESOURCES:
        raise EvidenceMeshError("mesh.resources must be a bounded non-empty array")
    resource_ids: set[str] = set()
    for index, resource in enumerate(resources):
        path = f"mesh.resources[{index}]"
        resource = _require_exact(resource, _RESOURCE_FIELDS, path)
        resource_id = _identifier(resource["resource_id"], f"{path}.resource_id")
        if resource_id in resource_ids:
            raise EvidenceMeshError("duplicate mesh resource")
        resource_ids.add(resource_id)
        if resource["project_id"] not in project_ids or resource["availability"] not in AVAILABILITY:
            raise EvidenceMeshError("resource project or availability is invalid")
        if resource["allowed_role"] not in ALLOWED_ROLES or resource["evidence_class"] not in EVIDENCE_CLASSES:
            raise EvidenceMeshError("resource role or evidence class is invalid")
        _url(resource["public_url"], f"{path}.public_url", nullable=True)
        _identifier(resource["independence_group"], f"{path}.independence_group")
        groups = resource["upstream_groups"]
        if type(groups) is not list or not groups or len(groups) > 64 or groups != sorted(set(groups)):
            raise EvidenceMeshError("resource upstream groups must be sorted, bounded and unique")
        for group in groups:
            _identifier(group, f"{path}.upstream_groups[]")
        if type(resource["independence_eligible"]) is not bool:
            raise EvidenceMeshError("resource independence eligibility must be boolean")
        rights = _require_exact(resource["rights"], _RIGHTS_FIELDS, f"{path}.rights")
        if rights["redistribution"] not in {
            "OPEN", "ATTRIBUTION_REQUIRED", "LINK_ONLY", "RESTRICTED", "UNKNOWN",
        } or rights["reuse"] not in {
            "prohibited", "metadata_only", "derived_only", "full_text",
        } or rights["training"] not in {
            "prohibited", "metadata_only", "derived_only", "full_text",
        }:
            raise EvidenceMeshError("resource rights disposition is invalid")
        clocks = _require_exact(resource["clocks"], _CLOCK_FIELDS, f"{path}.clocks")
        parsed_clocks = [
            _parse_timestamp(clocks[name], f"{path}.clocks.{name}") if clocks[name] is not None else None
            for name in ("event_time", "knowledge_time", "publication_time")
        ]
        known = [value for value in parsed_clocks if value is not None]
        if len(known) >= 2 and known != sorted(known):
            raise EvidenceMeshError("resource clocks are out of order")
        for name, value in zip(
            ("event_time", "knowledge_time", "publication_time"), parsed_clocks
        ):
            if name != "event_time" and value is not None and value > generated_at:
                raise EvidenceMeshError(f"resource {name} is after mesh generation")
        coverage = resource["source_temporal_coverage"]
        if coverage is not None:
            coverage = _require_exact(
                coverage, _SOURCE_TEMPORAL_COVERAGE_FIELDS,
                f"{path}.source_temporal_coverage",
            )
            if coverage["kind"] == "year_range":
                start = _safe_count(coverage["from_year"], f"{path}.coverage.from_year")
                end = _safe_count(coverage["to_year"], f"{path}.coverage.to_year")
                if not 1900 <= start <= end <= 2200 or coverage["snapshot_date"] is not None:
                    raise EvidenceMeshError("resource year-range coverage is inconsistent")
            elif coverage["kind"] == "snapshot":
                if coverage["from_year"] is not None or coverage["to_year"] is not None:
                    raise EvidenceMeshError("resource snapshot coverage has year bounds")
                _timestamp_or_date(
                    coverage["snapshot_date"], f"{path}.coverage.snapshot_date"
                )
            else:
                raise EvidenceMeshError("resource source temporal coverage kind is invalid")
        fresh = _require_exact(resource["freshness"], _FRESHNESS_FIELDS, f"{path}.freshness")
        if fresh["status"] not in FRESHNESS:
            raise EvidenceMeshError("unknown freshness status")
        if resource["availability"] == "unavailable" and fresh["status"] != "unavailable":
            raise EvidenceMeshError("unavailable resource must have unavailable freshness")
        if fresh["age_hours"] is not None and fresh["age_hours"] < 0:
            raise EvidenceMeshError("resource freshness age cannot be negative")
        if fresh["observed_at"] is None:
            if fresh["age_hours"] is not None or fresh["deadline"] is not None:
                raise EvidenceMeshError("freshness without observation must not imply an age")
        else:
            observed = _parse_timestamp(
                fresh["observed_at"], f"{path}.freshness.observed_at"
            )
            if observed > generated_at:
                raise EvidenceMeshError("freshness observation is after mesh generation")
            expected_age = round((generated_at - observed).total_seconds() / 3600.0, 3)
            if fresh["age_hours"] != expected_age:
                raise EvidenceMeshError("resource freshness age does not match its clocks")
            if fresh["deadline"] is not None:
                deadline = _parse_timestamp(
                    fresh["deadline"], f"{path}.freshness.deadline"
                )
                if deadline < observed:
                    raise EvidenceMeshError("freshness deadline precedes observation")
        if resource["input_id"] not in input_ids:
            raise EvidenceMeshError("resource references an unknown input receipt")
        if type(resource["limitations"]) is not list or not resource["limitations"] or len(resource["limitations"]) > 32:
            raise EvidenceMeshError("resource limitations must be bounded and non-empty")
    if [row["resource_id"] for row in resources] != sorted(resource_ids):
        raise EvidenceMeshError("mesh resources must be sorted")
    for resource in resources:
        if not set(resource["dependency_resource_ids"]) <= resource_ids:
            raise EvidenceMeshError("resource contains a dangling dependency")
    for project in projects:
        expected = sorted(row["resource_id"] for row in resources if row["project_id"] == project["id"])
        if project["resource_ids"] != expected:
            raise EvidenceMeshError("project resource index does not match resources")

    groups = document["dependency_groups"]
    if type(groups) is not list or len(groups) > MAX_RESOURCES:
        raise EvidenceMeshError("dependency groups must be bounded")
    group_ids: list[str] = []
    for index, group in enumerate(groups):
        group = _require_exact(group, _GROUP_FIELDS, f"mesh.dependency_groups[{index}]")
        group_id = _identifier(group["group_id"], f"mesh.dependency_groups[{index}].group_id")
        group_ids.append(group_id)
        expected_members = [row["resource_id"] for row in resources if group_id in row["upstream_groups"]]
        if group["resource_ids"] != expected_members:
            raise EvidenceMeshError("dependency group membership is inconsistent")
        if group["available_resource_count"] != sum(
            row["availability"] == "available" for row in resources if group_id in row["upstream_groups"]
        ):
            raise EvidenceMeshError("dependency group availability count is inconsistent")
        if group["independence_eligible_resource_count"] != sum(
            row["availability"] == "available" and row["independence_eligible"]
            for row in resources if group_id in row["upstream_groups"]
        ):
            raise EvidenceMeshError("dependency group independence count is inconsistent")
    if group_ids != sorted(set(group_ids)):
        raise EvidenceMeshError("dependency groups must be sorted and unique")

    summary = _require_exact(document["summary"], _SUMMARY_FIELDS, "mesh.summary")
    if summary["project_count"] != len(projects) or summary["resource_count"] != len(resources):
        raise EvidenceMeshError("mesh summary totals are inconsistent")
    expected_states = {
        "available_resource_count": sum(row["availability"] == "available" for row in resources),
        "stale_resource_count": sum(row["availability"] == "stale" for row in resources),
        "unavailable_resource_count": sum(row["availability"] == "unavailable" for row in resources),
    }
    for key, expected in expected_states.items():
        if summary[key] != expected:
            raise EvidenceMeshError(f"mesh summary {key} is inconsistent")
    expected_independence = len({
        row["independence_group"] for row in resources
        if row["availability"] == "available" and row["independence_eligible"]
    })
    if summary["independent_groups_available"] != expected_independence:
        raise EvidenceMeshError("mesh independent group summary is inconsistent")
    expected_optional_unavailable = sum(
        not receipt["required"] and receipt["availability"] == "unavailable"
        for receipt in inputs
    )
    if summary["optional_inputs_unavailable"] != expected_optional_unavailable:
        raise EvidenceMeshError("mesh optional input summary is inconsistent")
    for name, namespace in (("palimpsest_catalog", "catalog"), ("palimpsest_osint", "osint")):
        coverage = _require_exact(summary[name], _COVERAGE_FIELDS, f"mesh.summary.{name}")
        actual = sum(row["project_id"] == "palimpsest" and row["namespace"] == namespace for row in resources)
        if coverage["accounted"] != actual or coverage["expected"] != actual or coverage["complete"] is not True:
            raise EvidenceMeshError(f"mesh {namespace} coverage is incomplete")
    for field in ("project_resource_counts", "role_counts"):
        rows = summary[field]
        if type(rows) is not list:
            raise EvidenceMeshError(f"mesh.summary.{field} must be an array")
        for index, row in enumerate(rows):
            _require_exact(row, _COUNT_FIELDS, f"mesh.summary.{field}[{index}]")
            _safe_count(row["count"], f"mesh.summary.{field}[{index}].count")
    expected_project_counts = [
        {"id": project_id, "count": sum(row["project_id"] == project_id for row in resources)}
        for project_id in PROJECT_IDS
    ]
    if summary["project_resource_counts"] != expected_project_counts:
        raise EvidenceMeshError("mesh project resource counts are inconsistent")
    expected_role_counts = [
        {"id": role, "count": sum(row["allowed_role"] == role for row in resources)}
        for role in sorted(ALLOWED_ROLES)
    ]
    if summary["role_counts"] != expected_role_counts:
        raise EvidenceMeshError("mesh role counts are inconsistent")
    _scan_aggregate_only(document)


def write_evidence_mesh(document: Mapping[str, Any], path: Path | str) -> None:
    """Validate and atomically replace one evidence-mesh JSON document."""

    validate_evidence_mesh(document)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(document)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def check_evidence_mesh(
    path: Path | str,
    *,
    root: Path | str = ROOT,
    now: datetime | None = None,
    config_path: Path | str | None = None,
    partner_snapshot_paths: Mapping[str, Path | str] | None = None,
) -> bool:
    """Return whether an on-disk mesh equals a reproducible no-network rebuild."""

    existing_value, existing_raw = _load_json(Path(path), 16 * 1024 * 1024)
    validate_evidence_mesh(existing_value)
    build_time = now or _parse_timestamp(existing_value["generated_at"], "mesh.generated_at")
    expected = build_evidence_mesh(
        root, now=build_time, config_path=config_path,
        partner_snapshot_paths=partner_snapshot_paths,
    )
    return existing_raw == canonical_json_bytes(expected)


def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--now")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--seiche-snapshot", type=Path)
    parser.add_argument("--liquilens-snapshot", type=Path)
    args = parser.parse_args(argv)
    moment = _parse_timestamp(args.now, "--now") if args.now else None
    partners = {
        key: value for key, value in {
            "seiche": args.seiche_snapshot, "liquilens": args.liquilens_snapshot,
        }.items() if value is not None
    }
    if args.check:
        return 0 if check_evidence_mesh(
            args.output, root=args.root, now=moment, config_path=args.config,
            partner_snapshot_paths=partners,
        ) else 1
    document = build_evidence_mesh(
        args.root, now=moment, config_path=args.config, partner_snapshot_paths=partners
    )
    write_evidence_mesh(document, args.output)
    return 0


__all__ = [
    "CONFIG_SCHEMA_VERSION", "DEFAULT_CONFIG_PATH", "DEFAULT_OUTPUT_PATH",
    "EVIDENCE_CLASSES", "EvidenceMeshError", "SCHEMA_VERSION", "build_evidence_mesh",
    "canonical_json_bytes", "check_evidence_mesh", "validate_evidence_mesh",
    "write_evidence_mesh",
]


if __name__ == "__main__":
    raise SystemExit(_cli())
