"""Rights-gated China economic context export for Seiche.

The economic observation ledger is evidence, not permission to redistribute a
value.  This module authenticates the ledger, resolves every source against an
exact policy snapshot, and emits only rows whose value and Seiche-export gates
are both open.  Unknown and expired decisions are effective denials.

The JSONL rows embed the unchanged EconomicObservation v1 record.  Its economic
period, publisher-release clock, Palimpsest-collection clock, revision lineage,
and observation identity therefore survive the transport without reinterpretation.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time as daytime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit

from core.econ_ledger import load_snapshot
from core.econ_observation import EconomicObservation


POLICY_SCHEMA = "palimpsest.china-economic-source-policy.v1"
ARTIFACT_SCHEMA = "palimpsest.china-economic-export.v1"
MANIFEST_SCHEMA = "palimpsest.china-economic-export-manifest.v1"
POLICY_SCOPE = "china_economic_values_and_seiche_export"
WDI_REGISTRY_SCHEMA = "palimpsest-china-econ-wdi-series.v1"
WDI_SOURCE_ID = "world_bank_wdi"
MAX_POLICY_BYTES = 256 * 1024
MAX_SERIES_REGISTRY_BYTES = 2 * 1024 * 1024
_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_]{1,79}$")
_SERIES_ID = re.compile(r"^cn\.wdi\.[a-z0-9][a-z0-9_]{1,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MARKET_CHANNELS = frozenset({"money_market", "capital_market"})
_WDI_DATASET_FIELDS = frozenset(
    {
        "source_id",
        "source_number",
        "name",
        "publisher",
        "country_code",
        "api_base",
        "catalog_url",
        "license",
        "license_url",
        "rights_evidence_url",
        "redistribution_status",
        "independence_group",
        "release_time_semantics",
        "attribution",
        "per_indicator_upstream_metadata_status",
        "per_indicator_upstream_metadata_requirement",
    }
)
_WDI_SERIES_FIELDS = frozenset(
    {
        "indicator_id",
        "series_id",
        "name",
        "unit",
        "domain",
        "market_channels",
        "quality",
    }
)
_WDI_METADATA_FIELDS = frozenset(
    {
        "family",
        "source_series_id",
        "source_document_version",
        "parser_version",
        "release_time_semantics",
        "aggregation_window",
    }
)
_POLICY_FIELDS = frozenset(
    {
        "source_id",
        "decision",
        "values_allowed",
        "seiche_export_allowed",
        "license",
        "license_url",
        "rights_evidence_url",
        "attribution",
        "reviewed_at",
        "expires_at",
        "reason",
    }
)
_SOURCE_DECISION_FIELDS = frozenset(
    {
        *_POLICY_FIELDS,
        "decision_sha256",
        "input_records",
        "exported_records",
    }
)


class ChinaEconExportError(ValueError):
    """The policy, source registry, ledger, or export failed closed."""


@dataclass(frozen=True, slots=True)
class SourcePolicyDecision:
    source_id: str
    decision: str
    values_allowed: bool
    seiche_export_allowed: bool
    license: str | None
    license_url: str | None
    rights_evidence_url: str | None
    attribution: str | None
    reviewed_at: str
    expires_at: str
    reason: str
    reviewed_at_value: datetime
    expires_at_value: datetime
    decision_sha256: str


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    decisions: Mapping[str, SourcePolicyDecision]
    byte_size: int
    byte_sha256: str


@dataclass(frozen=True, slots=True)
class MarketBinding:
    series_id: str
    source_series_id: str
    name: str
    unit: str
    domain: str
    market_channels: tuple[str, ...]
    quality: float


@dataclass(frozen=True, slots=True)
class MarketRegistry:
    dataset: Mapping[str, Any]
    bindings: Mapping[str, MarketBinding]
    byte_size: int
    byte_sha256: str


@dataclass(frozen=True, slots=True)
class ExportBundle:
    artifact_bytes: bytes
    manifest: Mapping[str, Any]
    manifest_bytes: bytes


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic strict UTF-8 JSON with one terminal newline."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ChinaEconExportError(f"value is not canonical JSON: {exc}") from exc
    return (encoded + "\n").encode("utf-8")


def _strict_json(raw: bytes, *, label: str, maximum: int) -> Any:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        raise ChinaEconExportError(f"{label} is empty or exceeds {maximum} bytes")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ChinaEconExportError(f"{label} is not strict UTF-8") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ChinaEconExportError(f"{label} contains duplicate key {key!r}")
            out[key] = value
        return out

    def reject_constant(value: str) -> None:
        raise ChinaEconExportError(f"{label} contains non-finite number {value}")

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ChinaEconExportError(f"{label} is not valid JSON") from exc


def _timestamp(value: object, *, path: str) -> tuple[str, datetime]:
    if type(value) is not str or not value.endswith("Z"):
        raise ChinaEconExportError(f"{path} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ChinaEconExportError(f"{path} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ChinaEconExportError(f"{path} must be timezone-aware")
    normalized = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if normalized != value:
        raise ChinaEconExportError(f"{path} is not canonically encoded")
    return normalized, parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ChinaEconExportError("generated_at must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_text(value: object, *, path: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 1000:
        raise ChinaEconExportError(f"{path} must be null or a bounded non-empty string")
    return value


def _optional_https(value: object, *, path: str) -> str | None:
    parsed_value = _optional_text(value, path=path)
    if parsed_value is None:
        return None
    parsed = urlsplit(parsed_value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ChinaEconExportError(f"{path} must be a credential-free HTTPS URL")
    return parsed_value


def _decision_payload(decision: SourcePolicyDecision) -> dict[str, Any]:
    return {
        "source_id": decision.source_id,
        "decision": decision.decision,
        "values_allowed": decision.values_allowed,
        "seiche_export_allowed": decision.seiche_export_allowed,
        "license": decision.license,
        "license_url": decision.license_url,
        "rights_evidence_url": decision.rights_evidence_url,
        "attribution": decision.attribution,
        "reviewed_at": decision.reviewed_at,
        "expires_at": decision.expires_at,
        "reason": decision.reason,
    }


def _parse_source_policy(raw: bytes) -> SourcePolicy:
    """Validate one exact source-policy byte sequence.

    The current contract deliberately admits one value source only.  Adding a
    second allow decision is a policy and code review, not a configuration typo.
    """

    value = _strict_json(raw, label="China economic source policy", maximum=MAX_POLICY_BYTES)
    expected_top = {"schema_version", "policy_scope", "default_decision", "sources"}
    if type(value) is not dict or set(value) != expected_top:
        raise ChinaEconExportError("source policy has unexpected top-level fields")
    if value["schema_version"] != POLICY_SCHEMA:
        raise ChinaEconExportError(f"source policy must use {POLICY_SCHEMA}")
    if value["policy_scope"] != POLICY_SCOPE or value["default_decision"] != "deny":
        raise ChinaEconExportError("source policy scope must be exact and default-deny")
    sources = value["sources"]
    if type(sources) is not list or not sources or len(sources) > 256:
        raise ChinaEconExportError("source policy requires 1..256 source decisions")

    decisions: dict[str, SourcePolicyDecision] = {}
    for position, row in enumerate(sources, 1):
        path_name = f"sources[{position}]"
        if type(row) is not dict or set(row) != _POLICY_FIELDS:
            raise ChinaEconExportError(f"{path_name} has unexpected fields")
        source_id = row["source_id"]
        if type(source_id) is not str or not _SOURCE_ID.fullmatch(source_id):
            raise ChinaEconExportError(f"{path_name}.source_id is invalid")
        if source_id in decisions:
            raise ChinaEconExportError(f"duplicate source policy decision for {source_id}")
        decision = row["decision"]
        if decision not in {"allow", "deny"}:
            raise ChinaEconExportError(f"{path_name}.decision must be allow or deny")
        values_allowed = row["values_allowed"]
        export_allowed = row["seiche_export_allowed"]
        if type(values_allowed) is not bool or type(export_allowed) is not bool:
            raise ChinaEconExportError(f"{path_name} permission flags must be booleans")
        expected_permission = decision == "allow"
        if values_allowed != expected_permission or export_allowed != expected_permission:
            raise ChinaEconExportError(
                f"{path_name} permission flags must exactly match its decision"
            )
        if decision == "allow" and source_id != WDI_SOURCE_ID:
            raise ChinaEconExportError("only world_bank_wdi may have an allow decision")

        reviewed_at, reviewed_value = _timestamp(
            row["reviewed_at"], path=f"{path_name}.reviewed_at"
        )
        expires_at, expires_value = _timestamp(
            row["expires_at"], path=f"{path_name}.expires_at"
        )
        if expires_value <= reviewed_value:
            raise ChinaEconExportError(f"{path_name}.expires_at must follow reviewed_at")
        license_id = _optional_text(row["license"], path=f"{path_name}.license")
        license_url = _optional_https(row["license_url"], path=f"{path_name}.license_url")
        rights_url = _optional_https(
            row["rights_evidence_url"], path=f"{path_name}.rights_evidence_url"
        )
        attribution = _optional_text(
            row["attribution"], path=f"{path_name}.attribution"
        )
        reason = _optional_text(row["reason"], path=f"{path_name}.reason")
        if reason is None:
            raise ChinaEconExportError(f"{path_name}.reason is required")
        if decision == "allow" and None in (
            license_id,
            license_url,
            rights_url,
            attribution,
        ):
            raise ChinaEconExportError(f"{path_name} allow decision lacks rights evidence")
        if source_id == WDI_SOURCE_ID and decision == "allow":
            if license_id != "CC-BY-4.0" or license_url != (
                "https://creativecommons.org/licenses/by/4.0/"
            ):
                raise ChinaEconExportError("world_bank_wdi requires the reviewed CC-BY-4.0 grant")

        provisional = SourcePolicyDecision(
            source_id=source_id,
            decision=decision,
            values_allowed=values_allowed,
            seiche_export_allowed=export_allowed,
            license=license_id,
            license_url=license_url,
            rights_evidence_url=rights_url,
            attribution=attribution,
            reviewed_at=reviewed_at,
            expires_at=expires_at,
            reason=reason,
            reviewed_at_value=reviewed_value,
            expires_at_value=expires_value,
            decision_sha256="",
        )
        digest = hashlib.sha256(canonical_json_bytes(_decision_payload(provisional))).hexdigest()
        decisions[source_id] = replace(provisional, decision_sha256=digest)

    required = {WDI_SOURCE_ID, "cfets_benchmarks", "chinamoney"}
    if not required.issubset(decisions):
        raise ChinaEconExportError(
            "source policy must explicitly decide world_bank_wdi, cfets_benchmarks, and chinamoney"
        )
    if decisions[WDI_SOURCE_ID].decision != "allow" or any(
        decisions[source_id].decision != "deny"
        for source_id in ("cfets_benchmarks", "chinamoney")
    ):
        raise ChinaEconExportError("WDI must be allowed and CFETS/ChinaMoney denied")
    return SourcePolicy(
        decisions=decisions,
        byte_size=len(raw),
        byte_sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_source_policy(path: str | Path) -> SourcePolicy:
    """Load and validate the exact source-policy bytes."""

    return _parse_source_policy(Path(path).read_bytes())


def _parse_market_registry(raw: bytes) -> MarketRegistry:
    """Validate the exact reviewed WDI registry used to interpret export rows."""

    value = _strict_json(
        raw,
        label="WDI market-channel registry",
        maximum=MAX_SERIES_REGISTRY_BYTES,
    )
    if type(value) is not dict or set(value) != {"schema_version", "dataset", "series"}:
        raise ChinaEconExportError("WDI market-channel registry has unexpected fields")
    if value["schema_version"] != WDI_REGISTRY_SCHEMA:
        raise ChinaEconExportError(
            f"WDI market-channel registry must use {WDI_REGISTRY_SCHEMA}"
        )
    dataset, series = value["dataset"], value["series"]
    if (
        type(dataset) is not dict
        or set(dataset) != _WDI_DATASET_FIELDS
        or type(series) is not list
        or not series
        or len(series) > 256
    ):
        raise ChinaEconExportError("WDI market-channel registry has an invalid scope")
    required_dataset = {
        "source_id": WDI_SOURCE_ID,
        "source_number": "2",
        "country_code": "CHN",
        "api_base": "https://api.worldbank.org/v2",
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "redistribution_status": "allowed",
        "release_time_semantics": "dataset_lastupdated_upper_bound",
        "per_indicator_upstream_metadata_status": "residual_gate",
    }
    for key, expected in required_dataset.items():
        if dataset.get(key) != expected:
            raise ChinaEconExportError(
                f"WDI market-channel registry dataset.{key} must be {expected!r}"
            )
    for key in (
        "name",
        "publisher",
        "catalog_url",
        "rights_evidence_url",
        "independence_group",
        "attribution",
        "per_indicator_upstream_metadata_requirement",
    ):
        if type(dataset.get(key)) is not str or not dataset[key].strip():
            raise ChinaEconExportError(
                f"WDI market-channel registry dataset.{key} is required"
            )
    for key in ("catalog_url", "rights_evidence_url"):
        if _optional_https(dataset[key], path=f"registry.dataset.{key}") is None:
            raise ChinaEconExportError(f"registry.dataset.{key} is required")

    bindings: dict[str, MarketBinding] = {}
    indicators: set[str] = set()
    for position, row in enumerate(series, 1):
        if type(row) is not dict or set(row) != _WDI_SERIES_FIELDS:
            raise ChinaEconExportError(f"WDI series {position} has unexpected fields")
        series_id = row["series_id"]
        source_series_id = row["indicator_id"]
        name = row["name"]
        unit = row["unit"]
        domain = row["domain"]
        channels = row["market_channels"]
        quality = row["quality"]
        if type(series_id) is not str or not _SERIES_ID.fullmatch(series_id):
            raise ChinaEconExportError(f"WDI series {position} has an invalid series_id")
        if series_id in bindings:
            raise ChinaEconExportError(f"duplicate WDI series_id {series_id}")
        if (
            type(source_series_id) is not str
            or not source_series_id.strip()
            or len(source_series_id) > 80
        ):
            raise ChinaEconExportError(f"WDI series {series_id} lacks indicator_id")
        if source_series_id in indicators:
            raise ChinaEconExportError(f"duplicate WDI indicator_id {source_series_id}")
        for field_name, text in (("name", name), ("unit", unit), ("domain", domain)):
            if type(text) is not str or not text.strip() or len(text) > 512:
                raise ChinaEconExportError(
                    f"WDI series {series_id} has invalid {field_name}"
                )
        if (
            type(channels) is not list
            or not channels
            or any(type(channel) is not str or channel not in _MARKET_CHANNELS for channel in channels)
            or len(channels) != len(set(channels))
        ):
            raise ChinaEconExportError(f"WDI series {series_id} has invalid market_channels")
        if (
            isinstance(quality, bool)
            or not isinstance(quality, (int, float))
            or not 0 <= float(quality) <= 1
        ):
            raise ChinaEconExportError(f"WDI series {series_id} has invalid quality")
        bindings[series_id] = MarketBinding(
            series_id=series_id,
            source_series_id=source_series_id,
            name=name,
            unit=unit,
            domain=domain,
            market_channels=tuple(sorted(channels)),
            quality=float(quality),
        )
        indicators.add(source_series_id)
    return MarketRegistry(
        dataset=dict(dataset),
        bindings=bindings,
        byte_size=len(raw),
        byte_sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_market_registry(path: str | Path) -> MarketRegistry:
    """Load the exact reviewed WDI registry."""

    return _parse_market_registry(Path(path).read_bytes())


def load_market_bindings(path: str | Path) -> Mapping[str, MarketBinding]:
    """Compatibility helper returning the registry's series bindings."""

    return load_market_registry(path).bindings


def _effective_decision(
    configured: SourcePolicyDecision | None,
    *,
    evaluated_at: datetime,
) -> str:
    if configured is None:
        return "unknown"
    if evaluated_at < configured.reviewed_at_value:
        raise ChinaEconExportError(
            f"policy decision for {configured.source_id} is not effective yet"
        )
    if evaluated_at >= configured.expires_at_value:
        return "expired"
    return "allowed" if configured.decision == "allow" else "denied"


def _canonical_wdi_url(
    registry: MarketRegistry,
    *,
    start_year: int,
    end_year: int,
) -> str:
    codes = ";".join(
        sorted(binding.source_series_id for binding in registry.bindings.values())
    )
    query = urlencode(
        {
            "source": registry.dataset["source_number"],
            "date": f"{start_year}:{end_year}",
            "format": "json",
            "per_page": 20_000,
            "footnote": "y",
        }
    )
    return (
        f"{registry.dataset['api_base']}/country/"
        f"{registry.dataset['country_code']}/indicator/{codes}?{query}"
    )


def _wdi_request_years(evidence_url: str, registry: MarketRegistry) -> tuple[int, int]:
    try:
        parsed = urlsplit(evidence_url)
        query_rows = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ChinaEconExportError("WDI evidence_url is malformed") from exc
    query = dict(query_rows)
    if len(query_rows) != len(query) or set(query) != {
        "source",
        "date",
        "format",
        "per_page",
        "footnote",
    }:
        raise ChinaEconExportError("WDI evidence_url query is not the reviewed request")
    match = re.fullmatch(r"(\d{4}):(\d{4})", query["date"])
    if match is None:
        raise ChinaEconExportError("WDI evidence_url has an invalid year range")
    start_year, end_year = (int(value) for value in match.groups())
    if not 1960 <= start_year <= end_year <= 2100:
        raise ChinaEconExportError("WDI evidence_url year range is outside policy")
    expected = _canonical_wdi_url(
        registry,
        start_year=start_year,
        end_year=end_year,
    )
    if evidence_url != expected:
        raise ChinaEconExportError(
            "WDI evidence_url does not exactly bind the pinned registry and request"
        )
    return start_year, end_year


def _validate_wdi_observation(
    row: EconomicObservation,
    binding: MarketBinding,
    registry: MarketRegistry,
) -> None:
    start_year, end_year = _wdi_request_years(row.evidence_url, registry)
    if (
        row.series_id != binding.series_id
        or row.unit != binding.unit
        or row.quality != binding.quality
    ):
        raise ChinaEconExportError(
            f"{row.series_id} contract drifted from the pinned WDI registry"
        )
    if (
        row.source_id != WDI_SOURCE_ID
        or row.geography != "CN"
        or row.frequency != "A"
        or row.status != "estimate"
        or row.sector != "all"
        or row.firm_size != "all"
        or row.ownership != "all"
        or row.raw_sha256 is None
        or not _SHA256.fullmatch(row.raw_sha256)
    ):
        raise ChinaEconExportError(
            f"{row.series_id} is not a complete China WDI observation"
        )
    if (
        row.period_start != date(row.period_start.year, 1, 1)
        or row.period_end != date(row.period_start.year, 12, 31)
        or not start_year <= row.period_start.year <= end_year
    ):
        raise ChinaEconExportError(
            f"{row.series_id} period is outside its exact WDI request"
        )
    metadata = row.metadata
    if set(metadata) != _WDI_METADATA_FIELDS:
        raise ChinaEconExportError(f"{row.series_id} WDI metadata fields changed")
    expected_metadata = {
        "family": registry.dataset["independence_group"],
        "source_series_id": binding.source_series_id,
        "parser_version": "world-bank-wdi-json.v1",
        "release_time_semantics": registry.dataset["release_time_semantics"],
        "aggregation_window": "calendar_year",
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise ChinaEconExportError(
            f"{row.series_id} metadata is not bound to the pinned WDI registry"
        )
    source_version = metadata.get("source_document_version")
    if type(source_version) is not str:
        raise ChinaEconExportError(f"{row.series_id} lacks a WDI dataset version")
    try:
        last_updated = date.fromisoformat(source_version)
    except ValueError as exc:
        raise ChinaEconExportError(
            f"{row.series_id} has an invalid WDI dataset version"
        ) from exc
    collected_at = row.collected_at.astimezone(UTC)
    if last_updated > collected_at.date():
        raise ChinaEconExportError(
            f"{row.series_id} WDI dataset version is in the future"
        )
    expected_release = min(
        datetime.combine(last_updated, daytime(23, 59, 59), tzinfo=UTC),
        collected_at,
    )
    if row.released_at.astimezone(UTC) != expected_release:
        raise ChinaEconExportError(
            f"{row.series_id} release clock violates WDI upper-bound semantics"
        )


def _source_decision_row(
    source_id: str,
    *,
    configured: SourcePolicyDecision | None,
    effective: str,
    input_records: int,
    exported_records: int,
) -> dict[str, Any]:
    if configured is None:
        return {
            "source_id": source_id,
            "decision": "unknown",
            "values_allowed": False,
            "seiche_export_allowed": False,
            "license": None,
            "license_url": None,
            "rights_evidence_url": None,
            "attribution": None,
            "reviewed_at": None,
            "expires_at": None,
            "reason": "No reviewed source-policy decision; default deny applies.",
            "decision_sha256": None,
            "input_records": input_records,
            "exported_records": 0,
        }
    allowed = effective == "allowed"
    return {
        "source_id": source_id,
        "decision": effective,
        "values_allowed": configured.values_allowed if allowed else False,
        "seiche_export_allowed": configured.seiche_export_allowed if allowed else False,
        "license": configured.license,
        "license_url": configured.license_url,
        "rights_evidence_url": configured.rights_evidence_url,
        "attribution": configured.attribution,
        "reviewed_at": configured.reviewed_at,
        "expires_at": configured.expires_at,
        "reason": configured.reason,
        "decision_sha256": configured.decision_sha256,
        "input_records": input_records,
        "exported_records": exported_records,
    }


def build_export(
    *,
    ledger_path: str | Path,
    policy_path: str | Path,
    series_registry_path: str | Path,
    generated_at: datetime,
    artifact_name: str,
) -> ExportBundle:
    """Build one deterministic, exact-byte-pinned Seiche context export."""

    generated_at_text = _format_timestamp(generated_at)
    evaluated_at = generated_at.astimezone(UTC)
    if evaluated_at > datetime.now(UTC):
        raise ChinaEconExportError("generated_at cannot be in the future")
    if Path(artifact_name).name != artifact_name or not artifact_name.endswith(".jsonl"):
        raise ChinaEconExportError("artifact_name must be a JSONL basename")
    ledger_location = Path(ledger_path)
    policy_location = Path(policy_path)
    series_registry_location = Path(series_registry_path)
    snapshot = load_snapshot(ledger_location)
    if snapshot.as_of is not None and snapshot.as_of > evaluated_at:
        raise ChinaEconExportError("generated_at precedes the newest collection clock")
    policy_bytes = policy_location.read_bytes()
    series_registry_bytes = series_registry_location.read_bytes()
    policy = _parse_source_policy(policy_bytes)
    registry = _parse_market_registry(series_registry_bytes)
    bindings = registry.bindings

    input_counts = Counter(row.source_id for row in snapshot.observations)
    exported_counts: Counter[str] = Counter()
    output_rows: list[dict[str, Any]] = []
    effective_by_source: dict[str, str] = {}
    all_sources = sorted(set(policy.decisions) | set(input_counts))
    for source_id in all_sources:
        effective_by_source[source_id] = _effective_decision(
            policy.decisions.get(source_id), evaluated_at=evaluated_at
        )

    for observation in snapshot.observations:
        if effective_by_source[observation.source_id] != "allowed":
            continue
        if observation.source_id != WDI_SOURCE_ID:
            raise ChinaEconExportError("an unsupported source resolved to allowed")
        binding = bindings.get(observation.series_id)
        if binding is None:
            raise ChinaEconExportError(
                f"allowed WDI series {observation.series_id} lacks a market-channel decision"
            )
        _validate_wdi_observation(observation, binding, registry)
        output_rows.append(
            {
                "schema_version": ARTIFACT_SCHEMA,
                "context_only": True,
                "scoring_allowed": False,
                "market_channels": list(binding.market_channels),
                "observation": observation.to_dict(),
            }
        )
        exported_counts[observation.source_id] += 1

    artifact_bytes = b"".join(canonical_json_bytes(row) for row in output_rows)
    source_decisions = [
        _source_decision_row(
            source_id,
            configured=policy.decisions.get(source_id),
            effective=effective_by_source[source_id],
            input_records=input_counts[source_id],
            exported_records=exported_counts[source_id],
        )
        for source_id in all_sources
    ]
    channel_mapping = {
        channel: sorted(
            {
                row["observation"]["series_id"]
                for row in output_rows
                if channel in row["market_channels"]
            }
        )
        for channel in sorted(_MARKET_CHANNELS)
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": generated_at_text,
        "context_only": True,
        "scoring_allowed": False,
        "artifact": {
            "path": artifact_name,
            "media_type": "application/x-ndjson",
            "schema_version": ARTIFACT_SCHEMA,
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "bytes": len(artifact_bytes),
            "records": len(output_rows),
        },
        "input_ledger": {
            "path": ledger_location.name,
            "sha256": snapshot.byte_sha256,
            "bytes": snapshot.byte_size,
            "records": snapshot.records,
        },
        "policy": {
            "path": policy_location.name,
            "sha256": policy.byte_sha256,
            "schema_version": POLICY_SCHEMA,
            "evaluated_at": generated_at_text,
        },
        "series_registry": {
            "path": series_registry_location.name,
            "sha256": registry.byte_sha256,
            "bytes": registry.byte_size,
            "schema_version": WDI_REGISTRY_SCHEMA,
        },
        "source_decisions": source_decisions,
        "market_channel_mapping": channel_mapping,
    }
    validate_export_bundle(
        artifact_bytes,
        manifest,
        policy_bytes=policy_bytes,
        series_registry_bytes=series_registry_bytes,
    )
    return ExportBundle(
        artifact_bytes=artifact_bytes,
        manifest=manifest,
        manifest_bytes=canonical_json_bytes(manifest),
    )


def validate_export_bundle(
    artifact_bytes: bytes,
    manifest: Mapping[str, Any],
    *,
    policy_bytes: bytes,
    series_registry_bytes: bytes,
) -> None:
    """Validate the wire contract against its exact policy and registry bytes."""

    expected_top = {
        "schema_version",
        "generated_at",
        "context_only",
        "scoring_allowed",
        "artifact",
        "input_ledger",
        "policy",
        "series_registry",
        "source_decisions",
        "market_channel_mapping",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != expected_top:
        raise ChinaEconExportError("export manifest has unexpected fields")
    if manifest["schema_version"] != MANIFEST_SCHEMA:
        raise ChinaEconExportError(f"export manifest must use {MANIFEST_SCHEMA}")
    _, generated_at = _timestamp(
        manifest["generated_at"], path="manifest.generated_at"
    )
    if generated_at > datetime.now(UTC):
        raise ChinaEconExportError("manifest.generated_at cannot be in the future")
    if manifest["context_only"] is not True or manifest["scoring_allowed"] is not False:
        raise ChinaEconExportError("export authority boundary changed")

    parsed_policy = _parse_source_policy(policy_bytes)
    parsed_registry = _parse_market_registry(series_registry_bytes)

    artifact = manifest["artifact"]
    if not isinstance(artifact, Mapping) or set(artifact) != {
        "path",
        "media_type",
        "schema_version",
        "sha256",
        "bytes",
        "records",
    }:
        raise ChinaEconExportError("manifest.artifact has unexpected fields")
    if (
        type(artifact["path"]) is not str
        or Path(artifact["path"]).name != artifact["path"]
        or artifact["media_type"] != "application/x-ndjson"
        or artifact["schema_version"] != ARTIFACT_SCHEMA
        or type(artifact["bytes"]) is not int
        or type(artifact["records"]) is not int
        or artifact["bytes"] < 0
        or artifact["records"] < 0
        or artifact["sha256"] != hashlib.sha256(artifact_bytes).hexdigest()
        or artifact["bytes"] != len(artifact_bytes)
    ):
        raise ChinaEconExportError("manifest.artifact does not authenticate exact bytes")

    lines = artifact_bytes.splitlines()
    if artifact_bytes and not artifact_bytes.endswith(b"\n"):
        raise ChinaEconExportError("artifact does not end at a JSONL record boundary")
    if artifact["records"] != len(lines):
        raise ChinaEconExportError("manifest.artifact record count does not match")
    decoded_rows: list[dict[str, Any]] = []
    observation_ids: set[str] = set()
    artifact_counts: Counter[str] = Counter()
    for position, line in enumerate(lines, 1):
        row = _strict_json(line, label=f"artifact row {position}", maximum=1024 * 1024)
        if canonical_json_bytes(row) != line + b"\n":
            raise ChinaEconExportError(f"artifact row {position} is not canonical JSON")
        if type(row) is not dict or set(row) != {
            "schema_version",
            "context_only",
            "scoring_allowed",
            "market_channels",
            "observation",
        }:
            raise ChinaEconExportError(f"artifact row {position} has unexpected fields")
        channels = row["market_channels"]
        if (
            row["schema_version"] != ARTIFACT_SCHEMA
            or row["context_only"] is not True
            or row["scoring_allowed"] is not False
            or type(channels) is not list
            or not channels
            or channels != sorted(channels)
            or len(channels) != len(set(channels))
            or any(channel not in _MARKET_CHANNELS for channel in channels)
        ):
            raise ChinaEconExportError(f"artifact row {position} violates its authority contract")
        observation_value = row["observation"]
        if type(observation_value) is not dict or "observation_id" not in observation_value:
            raise ChinaEconExportError(f"artifact row {position} lacks an observation")
        supplied_id = observation_value["observation_id"]
        try:
            observation = EconomicObservation.from_dict(observation_value)
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ChinaEconExportError(f"artifact row {position} is not observation v1: {exc}") from exc
        if observation.observation_id != supplied_id or observation.source_id != WDI_SOURCE_ID:
            raise ChinaEconExportError(f"artifact row {position} observation identity is invalid")
        if observation.collected_at.astimezone(UTC) > generated_at:
            raise ChinaEconExportError(
                f"artifact row {position} was collected after manifest.generated_at"
            )
        if supplied_id in observation_ids:
            raise ChinaEconExportError(f"artifact row {position} duplicates observation_id")
        binding = parsed_registry.bindings.get(observation.series_id)
        if binding is None:
            raise ChinaEconExportError(
                f"artifact row {position} series is absent from the pinned registry"
            )
        if channels != list(binding.market_channels):
            raise ChinaEconExportError(
                f"artifact row {position} market channels drifted from the pinned registry"
            )
        _validate_wdi_observation(observation, binding, parsed_registry)
        observation_ids.add(supplied_id)
        artifact_counts[observation.source_id] += 1
        decoded_rows.append(row)

    input_ledger = manifest["input_ledger"]
    if not isinstance(input_ledger, Mapping) or set(input_ledger) != {
        "path",
        "sha256",
        "bytes",
        "records",
    }:
        raise ChinaEconExportError("manifest.input_ledger has unexpected fields")
    if (
        type(input_ledger["path"]) is not str
        or Path(input_ledger["path"]).name != input_ledger["path"]
        or type(input_ledger["bytes"]) is not int
        or input_ledger["bytes"] < 0
        or type(input_ledger["records"]) is not int
        or input_ledger["records"] < 0
        or type(input_ledger["sha256"]) is not str
        or not _SHA256.fullmatch(input_ledger["sha256"])
    ):
        raise ChinaEconExportError("manifest.input_ledger receipt is invalid")

    policy = manifest["policy"]
    if not isinstance(policy, Mapping) or set(policy) != {
        "path",
        "sha256",
        "schema_version",
        "evaluated_at",
    }:
        raise ChinaEconExportError("manifest.policy has unexpected fields")
    if (
        type(policy["path"]) is not str
        or Path(policy["path"]).name != policy["path"]
        or type(policy["sha256"]) is not str
        or not _SHA256.fullmatch(policy["sha256"])
        or policy["sha256"] != parsed_policy.byte_sha256
        or policy["schema_version"] != POLICY_SCHEMA
        or policy["evaluated_at"] != manifest["generated_at"]
    ):
        raise ChinaEconExportError("manifest.policy receipt is invalid")

    series_registry = manifest["series_registry"]
    if not isinstance(series_registry, Mapping) or set(series_registry) != {
        "path",
        "sha256",
        "bytes",
        "schema_version",
    }:
        raise ChinaEconExportError("manifest.series_registry has unexpected fields")
    if (
        type(series_registry["path"]) is not str
        or Path(series_registry["path"]).name != series_registry["path"]
        or type(series_registry["sha256"]) is not str
        or series_registry["sha256"] != parsed_registry.byte_sha256
        or type(series_registry["bytes"]) is not int
        or series_registry["bytes"] != parsed_registry.byte_size
        or series_registry["schema_version"] != WDI_REGISTRY_SCHEMA
    ):
        raise ChinaEconExportError("manifest.series_registry receipt is invalid")

    decisions = manifest["source_decisions"]
    if type(decisions) is not list or not decisions:
        raise ChinaEconExportError("manifest.source_decisions must be non-empty")
    decision_sources: list[str] = []
    exported_by_source: Counter[str] = Counter()
    for position, row in enumerate(decisions, 1):
        if type(row) is not dict or set(row) != _SOURCE_DECISION_FIELDS:
            raise ChinaEconExportError(f"source_decisions[{position}] has unexpected fields")
        source_id = row["source_id"]
        if type(source_id) is not str or not _SOURCE_ID.fullmatch(source_id):
            raise ChinaEconExportError(f"source_decisions[{position}].source_id is invalid")
        if row["decision"] not in {"allowed", "denied", "expired", "unknown"}:
            raise ChinaEconExportError(f"source_decisions[{position}].decision is invalid")
        allowed = row["decision"] == "allowed"
        if type(row["values_allowed"]) is not bool or type(row["seiche_export_allowed"]) is not bool:
            raise ChinaEconExportError(f"source_decisions[{position}] permission flags are invalid")
        if allowed and not (row["values_allowed"] and row["seiche_export_allowed"]):
            raise ChinaEconExportError(f"source_decisions[{position}] permission flags disagree")
        if not allowed and (row["values_allowed"] or row["seiche_export_allowed"]):
            raise ChinaEconExportError(f"source_decisions[{position}] permission flags disagree")
        if allowed and (
            source_id != WDI_SOURCE_ID
            or row["license"] != "CC-BY-4.0"
            or row["license_url"] != "https://creativecommons.org/licenses/by/4.0/"
        ):
            raise ChinaEconExportError("only CC-BY-4.0 world_bank_wdi may be allowed")
        if row["decision"] == "unknown":
            if any(
                row[field] is not None
                for field in (
                    "decision_sha256",
                    "license",
                    "license_url",
                    "rights_evidence_url",
                    "attribution",
                    "reviewed_at",
                    "expires_at",
                )
            ):
                raise ChinaEconExportError("unknown source decision contains invented policy data")
        elif type(row["decision_sha256"]) is not str or not _SHA256.fullmatch(
            row["decision_sha256"]
        ):
            raise ChinaEconExportError("configured source decision lacks a digest")
        elif (
            type(row["reviewed_at"]) is not str
            or type(row["expires_at"]) is not str
            or type(row["reason"]) is not str
            or not row["reason"].strip()
        ):
            raise ChinaEconExportError("configured source decision lacks review metadata")
        for count_field in ("input_records", "exported_records"):
            if type(row[count_field]) is not int or row[count_field] < 0:
                raise ChinaEconExportError(f"source_decisions[{position}].{count_field} is invalid")
        if not allowed and row["exported_records"] != 0:
            raise ChinaEconExportError("a denied source reports exported values")
        configured = parsed_policy.decisions.get(source_id)
        effective = _effective_decision(configured, evaluated_at=generated_at)
        expected_decision = _source_decision_row(
            source_id,
            configured=configured,
            effective=effective,
            input_records=row["input_records"],
            exported_records=row["exported_records"],
        )
        if row != expected_decision:
            raise ChinaEconExportError(
                f"source_decisions[{position}] does not match the pinned policy"
            )
        if row["exported_records"] != artifact_counts[source_id]:
            raise ChinaEconExportError(
                f"source_decisions[{position}] export count does not match artifact rows"
            )
        decision_sources.append(source_id)
        exported_by_source[source_id] += row["exported_records"]
    if decision_sources != sorted(set(decision_sources)):
        raise ChinaEconExportError("source decisions are not uniquely sorted")
    if not set(parsed_policy.decisions).issubset(decision_sources):
        raise ChinaEconExportError("source decisions omit a pinned policy boundary")
    if sum(row["input_records"] for row in decisions) != input_ledger["records"]:
        raise ChinaEconExportError("source-decision input counts do not reconcile")
    if sum(exported_by_source.values()) != artifact["records"]:
        raise ChinaEconExportError("source-decision export counts do not reconcile")

    mapping = manifest["market_channel_mapping"]
    if not isinstance(mapping, Mapping) or set(mapping) != _MARKET_CHANNELS:
        raise ChinaEconExportError("market_channel_mapping has unexpected fields")
    expected_mapping = {
        channel: sorted(
            {
                row["observation"]["series_id"]
                for row in decoded_rows
                if channel in row["market_channels"]
            }
        )
        for channel in sorted(_MARKET_CHANNELS)
    }
    if dict(mapping) != expected_mapping:
        raise ChinaEconExportError("market_channel_mapping does not match artifact rows")


__all__ = [
    "ARTIFACT_SCHEMA",
    "MANIFEST_SCHEMA",
    "POLICY_SCHEMA",
    "ChinaEconExportError",
    "ExportBundle",
    "MarketRegistry",
    "build_export",
    "canonical_json_bytes",
    "load_market_bindings",
    "load_market_registry",
    "load_source_policy",
    "validate_export_bundle",
]
