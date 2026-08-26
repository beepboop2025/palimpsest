"""Public, aggregate-only contract for reviewed UCDP annual bulk data.

This contract is intentionally narrower than the upstream datasets. It exposes
annual conflict/territory/actor identifiers and country-year uncertainty bounds.
It cannot carry event coordinates, village names, narratives, person records,
live tactical fields, or any inference connecting conflict actors to drugs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from core.bri_observation import canonical_json_bytes, sha256_bytes

BUNDLE_SCHEMA_VERSION = "palimpsest.ucdp-aggregate.v1"
CONFLICT_RECORD_SCHEMA_VERSION = "palimpsest.ucdp-conflict-year.v1"
COUNTRY_RECORD_SCHEMA_VERSION = "palimpsest.ucdp-country-year.v1"
DATASET_VERSION = "26.1"
GEOGRAPHIES = frozenset({"PAK-BAL", "MMR"})
COUNTRY_CODES = frozenset({"PAK", "MMR"})
MYANMAR_TERRITORIES = frozenset(
    {
        "Arakan",
        "Common Border",
        "Kachin",
        "Karen",
        "Karenni",
        "Kokang",
        "Lahu",
        "Mon",
        "Nagaland",
        "Shan",
        "Wa",
    }
)
MAX_CONFLICT_RECORDS = 2_000
MAX_COUNTRY_RECORDS = 200
MAX_ACTOR_IDS_PER_SIDE = 64

TRUST_MODEL = (
    "palimpsest_reviewed_git_lock_for_hardened_tls_acquisition_no_upstream_signature"
)
PUBLICATION_SCOPE = "annual_aggregate_context_only"
PUBLIC_SOURCE_FIELDS = (
    "source_id",
    "name",
    "publisher",
    "dataset_version",
    "catalog_url",
    "license",
    "license_url",
    "rights_evidence_url",
    "redistribution_status",
    "attribution",
    "source_period_start_year",
    "source_period_end_year",
    "release_cadence",
    "rights_decision_id",
    "rights_observed_at",
    "rights_reviewed_at",
    "rights_valid_until",
    "review_lock_sha256",
    "trust_model",
    "citations",
)
PUBLIC_RECEIPT_FIELDS = (
    "schema_version",
    "acquisition_id",
    "input_id",
    "dataset_version",
    "source_url",
    "request_method",
    "request_user_agent",
    "redirect_policy",
    "tls_verification",
    "maximum_archive_bytes",
    "maximum_member_bytes",
    "maximum_source_age_days",
    "archive_sha256",
    "archive_bytes",
    "member_name",
    "member_sha256",
    "member_bytes",
    "http_last_modified",
    "retrieved_at",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "actor_name",
        "address",
        "coordinates",
        "date_end",
        "date_start",
        "drug_actor",
        "event_date",
        "event_description",
        "event_id",
        "event_text",
        "latitude",
        "longitude",
        "name_data",
        "narrative",
        "person_id",
        "person_name",
        "route",
        "source_article",
        "source_headline",
        "village",
        "side_a",
        "side_b",
        "side_a_2nd",
        "side_b_2nd",
        "govt_name",
        "name_orig",
        "name_orig_full",
        "name_orig_full_eng",
        "name_change",
        "new_name",
        "new_name_full_mother_tongue",
        "new_name_full_eng",
        "name_prev",
        "name_split_temp",
        "name_alliance",
        "group_name",
        "sb_dyad_names",
        "sb_intrastate_dyad_names",
        "sb_interstate_dyad_names",
        "ns_dyad_names",
    }
)
_FORBIDDEN_PUBLIC_KEY_TOKENS = frozenset(
    re.sub(r"[^a-z0-9]", "", value.casefold()) for value in _FORBIDDEN_PUBLIC_KEYS
)


class UCDPAggregateError(ValueError):
    """A UCDP public aggregate violated the reviewed contract."""


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object, *, label: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise UCDPAggregateError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise UCDPAggregateError(f"{label} must be a canonical UTC timestamp") from exc
    if _timestamp(parsed) != value:
        raise UCDPAggregateError(f"{label} must be a canonical UTC timestamp")
    return parsed


def _utc(value: object, *, label: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise UCDPAggregateError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: object, *, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise UCDPAggregateError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(
    value: object,
    *,
    label: str,
    maximum_bytes: int = 512,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        qualifier = "text" if allow_empty else "non-empty text"
        raise UCDPAggregateError(f"{label} must be {qualifier}")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise UCDPAggregateError(f"{label} exceeds {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise UCDPAggregateError(f"{label} contains control characters")
    return value


def _integer(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int = 2_147_483_647,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise UCDPAggregateError(
            f"{label} must be an integer from {minimum} through {maximum}"
        )
    return value


def _actor_ids(value: object, *, label: str) -> tuple[int, ...]:
    if type(value) not in (tuple, list) or not value:
        raise UCDPAggregateError(f"{label} must be a non-empty actor ID sequence")
    normalized = tuple(
        _integer(item, label=f"{label} item", minimum=1) for item in value
    )
    if normalized != tuple(sorted(set(normalized))):
        raise UCDPAggregateError(f"{label} must be sorted and unique")
    if len(normalized) > MAX_ACTOR_IDS_PER_SIDE:
        raise UCDPAggregateError(
            f"{label} exceeds the public {MAX_ACTOR_IDS_PER_SIDE}-ID cap"
        )
    return normalized


def assert_public_safe(
    value: object,
    *,
    forbidden_values: Sequence[str] = (),
) -> None:
    """Recursively refuse raw-field keys and private source values.

    The JSON Schema is the primary closed shape.  This independent scrub is a
    final alarm for source-only names or narrative values accidentally routed
    through an otherwise allowed string field.
    """

    normalized_forbidden: set[str] = set()
    for position, candidate in enumerate(forbidden_values, 1):
        if type(candidate) is not str:
            raise TypeError(f"forbidden value {position} must be text")
        stripped = candidate.strip()
        if stripped:
            normalized_forbidden.add(stripped.casefold())

    def visit(current: object) -> None:
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if type(key) is not str:
                    raise UCDPAggregateError("public object keys must be text")
                token = re.sub(r"[^a-z0-9]", "", key.casefold())
                if token in _FORBIDDEN_PUBLIC_KEY_TOKENS:
                    raise UCDPAggregateError(f"prohibited public field: {key}")
                visit(nested)
        elif isinstance(current, (list, tuple)):
            for nested in current:
                visit(nested)
        elif type(current) is str:
            candidate = current.strip().casefold()
            if candidate and candidate in normalized_forbidden:
                raise UCDPAggregateError("prohibited private source value reached public output")
            for sentinel in normalized_forbidden:
                if len(sentinel) >= 16 and sentinel in candidate:
                    raise UCDPAggregateError(
                        "prohibited private source value reached public output"
                    )

    visit(value)


@dataclass(frozen=True, slots=True)
class CitationRecord:
    citation_id: str
    title: str
    url: str
    applies_to: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.citation_id, label="citation_id", maximum_bytes=128)
        _text(self.title, label="citation title", maximum_bytes=512)
        if type(self.url) is not str or not self.url.startswith("https://"):
            raise UCDPAggregateError("citation URL must be HTTPS")
        if type(self.applies_to) is not tuple or not self.applies_to:
            raise UCDPAggregateError("citation applies_to must be a non-empty tuple")
        if self.applies_to != tuple(sorted(set(self.applies_to))):
            raise UCDPAggregateError("citation applies_to must be sorted and unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "citation_id": self.citation_id,
            "title": self.title,
            "url": self.url,
            "applies_to": list(self.applies_to),
        }


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    name: str
    publisher: str
    dataset_version: str
    catalog_url: str
    license: str
    license_url: str
    rights_evidence_url: str
    redistribution_status: str
    attribution: str
    source_period_start_year: int
    source_period_end_year: int
    release_cadence: str
    rights_decision_id: str
    rights_observed_at: str
    rights_reviewed_at: str
    rights_valid_until: str
    review_lock_sha256: str
    trust_model: str
    citations: tuple[CitationRecord, ...]

    def __post_init__(self) -> None:
        fixed = {
            "source_id": (self.source_id, "ucdp_bulk_26_1"),
            "name": (
                self.name,
                "Uppsala Conflict Data Program annual bulk datasets",
            ),
            "publisher": (
                self.publisher,
                "Uppsala Conflict Data Program, Uppsala University",
            ),
            "dataset_version": (self.dataset_version, DATASET_VERSION),
            "catalog_url": (self.catalog_url, "https://ucdp.uu.se/downloads/"),
            "license": (self.license, "CC-BY-4.0"),
            "license_url": (
                self.license_url,
                "https://creativecommons.org/licenses/by/4.0/",
            ),
            "rights_evidence_url": (
                self.rights_evidence_url,
                "https://ucdp.uu.se/downloads/",
            ),
            "redistribution_status": (
                self.redistribution_status,
                "allowed_with_attribution",
            ),
            "attribution": (
                self.attribution,
                "Uppsala Conflict Data Program (UCDP), version 26.1",
            ),
            "release_cadence": (self.release_cadence, "annual"),
            "trust_model": (self.trust_model, TRUST_MODEL),
        }
        for label, (actual, expected) in fixed.items():
            if actual != expected:
                raise UCDPAggregateError(f"public source {label} changed")
        if self.source_period_start_year != 1946 or self.source_period_end_year != 2025:
            raise UCDPAggregateError("public source period changed")
        _digest(self.rights_decision_id, label="rights_decision_id")
        _digest(self.review_lock_sha256, label="review_lock_sha256")
        observed = parse_timestamp(self.rights_observed_at, label="rights_observed_at")
        reviewed = parse_timestamp(self.rights_reviewed_at, label="rights_reviewed_at")
        valid_until = parse_timestamp(self.rights_valid_until, label="rights_valid_until")
        if not observed <= reviewed < valid_until:
            raise UCDPAggregateError("rights decision clocks are inconsistent")
        if type(self.citations) is not tuple or len(self.citations) != 3:
            raise UCDPAggregateError("public source requires three reviewed citations")
        if any(not isinstance(row, CitationRecord) for row in self.citations):
            raise TypeError("citations must contain CitationRecord")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "publisher": self.publisher,
            "dataset_version": self.dataset_version,
            "catalog_url": self.catalog_url,
            "license": self.license,
            "license_url": self.license_url,
            "rights_evidence_url": self.rights_evidence_url,
            "redistribution_status": self.redistribution_status,
            "attribution": self.attribution,
            "source_period_start_year": self.source_period_start_year,
            "source_period_end_year": self.source_period_end_year,
            "release_cadence": self.release_cadence,
            "rights_decision_id": self.rights_decision_id,
            "rights_observed_at": self.rights_observed_at,
            "rights_reviewed_at": self.rights_reviewed_at,
            "rights_valid_until": self.rights_valid_until,
            "review_lock_sha256": self.review_lock_sha256,
            "trust_model": self.trust_model,
            "citations": [row.to_dict() for row in self.citations],
        }


@dataclass(frozen=True, slots=True)
class AcquisitionReceiptRecord:
    schema_version: str
    acquisition_id: str
    input_id: str
    dataset_version: str
    source_url: str
    request_method: str
    request_user_agent: str
    redirect_policy: str
    tls_verification: str
    maximum_archive_bytes: int
    maximum_member_bytes: int
    maximum_source_age_days: int
    archive_sha256: str
    archive_bytes: int
    member_name: str
    member_sha256: str
    member_bytes: int
    http_last_modified: str
    retrieved_at: str

    def __post_init__(self) -> None:
        if self.schema_version != "palimpsest.ucdp-bulk-acquisition-receipt.v1":
            raise UCDPAggregateError("public acquisition schema changed")
        if self.input_id not in {
            "armed_conflict",
            "actor_registry",
            "organized_country_year",
        }:
            raise UCDPAggregateError("public acquisition input_id changed")
        if self.dataset_version != DATASET_VERSION:
            raise UCDPAggregateError("public acquisition dataset version changed")
        if self.request_method != "GET" or self.redirect_policy != "disabled":
            raise UCDPAggregateError("public acquisition request policy changed")
        if self.tls_verification != "required":
            raise UCDPAggregateError("public acquisition TLS policy changed")
        _digest(self.acquisition_id, label="acquisition_id")
        _digest(self.archive_sha256, label="archive_sha256")
        _digest(self.member_sha256, label="member_sha256")
        _integer(self.archive_bytes, label="archive_bytes", minimum=1)
        _integer(self.member_bytes, label="member_bytes", minimum=1)
        _integer(self.maximum_archive_bytes, label="maximum_archive_bytes", minimum=1)
        _integer(self.maximum_member_bytes, label="maximum_member_bytes", minimum=1)
        _integer(self.maximum_source_age_days, label="maximum_source_age_days", minimum=1)
        parse_timestamp(self.http_last_modified, label="http_last_modified")
        parse_timestamp(self.retrieved_at, label="retrieved_at")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "AcquisitionReceiptRecord":
        if type(value) is not dict or set(value) != set(PUBLIC_RECEIPT_FIELDS):
            raise UCDPAggregateError("public acquisition receipt shape changed")
        fields = {field: value[field] for field in PUBLIC_RECEIPT_FIELDS}
        return cls(**fields)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in PUBLIC_RECEIPT_FIELDS}


@dataclass(frozen=True, slots=True)
class ConflictYearAggregate:
    """One delayed annual conflict row with identifiers, never event details."""

    geography_code: str
    country_code: str
    territory_name: str | None
    year: int
    conflict_id: int
    side_a_actor_ids: tuple[int, ...]
    side_b_actor_ids: tuple[int, ...]
    source_row_sha256: str
    armed_conflict_acquisition_id: str
    actor_registry_acquisition_id: str
    dataset_version: str = DATASET_VERSION
    evidence_state: str = "observed"

    def __post_init__(self) -> None:
        if self.geography_code not in GEOGRAPHIES:
            raise UCDPAggregateError("conflict geography_code is outside scope")
        expected_country = "PAK" if self.geography_code == "PAK-BAL" else "MMR"
        if self.country_code != expected_country:
            raise UCDPAggregateError("conflict country/geography binding changed")
        if self.geography_code == "PAK-BAL":
            if self.territory_name != "Balochistan":
                raise UCDPAggregateError(
                    "Pakistan public conflict rows must be Balochistan-only"
                )
        elif (
            self.territory_name is not None
            and self.territory_name not in MYANMAR_TERRITORIES
        ):
            raise UCDPAggregateError(
                "Myanmar territory_name is outside the reviewed annual allowlist"
            )
        _integer(self.year, label="conflict year", minimum=1946, maximum=2025)
        _integer(self.conflict_id, label="conflict_id", minimum=1)
        side_a = _actor_ids(self.side_a_actor_ids, label="side_a_actor_ids")
        side_b = _actor_ids(self.side_b_actor_ids, label="side_b_actor_ids")
        if set(side_a) & set(side_b):
            raise UCDPAggregateError("the same actor cannot occupy both source sides")
        _digest(self.source_row_sha256, label="source_row_sha256")
        _digest(
            self.armed_conflict_acquisition_id,
            label="armed_conflict_acquisition_id",
        )
        _digest(
            self.actor_registry_acquisition_id,
            label="actor_registry_acquisition_id",
        )
        if self.dataset_version != DATASET_VERSION:
            raise UCDPAggregateError("conflict dataset_version changed")
        if self.evidence_state != "observed":
            raise UCDPAggregateError("conflict evidence_state must remain observed")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": CONFLICT_RECORD_SCHEMA_VERSION,
            "geography_code": self.geography_code,
            "country_code": self.country_code,
            "territory_name": self.territory_name,
            "year": self.year,
            "conflict_id": self.conflict_id,
            "side_a_actor_ids": list(self.side_a_actor_ids),
            "side_b_actor_ids": list(self.side_b_actor_ids),
            "dataset_version": self.dataset_version,
            "evidence_state": self.evidence_state,
            "source_row_sha256": self.source_row_sha256,
            "armed_conflict_acquisition_id": self.armed_conflict_acquisition_id,
            "actor_registry_acquisition_id": self.actor_registry_acquisition_id,
        }

    @property
    def record_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self._payload()))

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["record_id"] = self.record_id
        return payload


@dataclass(frozen=True, slots=True)
class UncertaintyBounds:
    low: int
    best: int
    high: int

    def __post_init__(self) -> None:
        low = _integer(self.low, label="uncertainty low")
        best = _integer(self.best, label="uncertainty best")
        high = _integer(self.high, label="uncertainty high")
        if not low <= best <= high:
            raise UCDPAggregateError(
                "uncertainty bounds must satisfy low <= best <= high"
            )

    def to_dict(self) -> dict[str, int]:
        return {"low": self.low, "best": self.best, "high": self.high}


@dataclass(frozen=True, slots=True)
class CountryYearAggregate:
    """Country-year UCDP fatality uncertainty, aggregated across categories."""

    country_code: str
    year: int
    state_based: UncertaintyBounds
    non_state: UncertaintyBounds
    one_sided: UncertaintyBounds
    total: UncertaintyBounds
    source_row_sha256: str
    country_year_acquisition_id: str
    dataset_version: str = DATASET_VERSION
    unit: str = "deaths"
    evidence_state: str = "observed"
    total_derivation: str = "sum_of_ucdp_category_bounds"

    def __post_init__(self) -> None:
        if self.country_code not in COUNTRY_CODES:
            raise UCDPAggregateError("country-year country_code is outside scope")
        _integer(self.year, label="country-year year", minimum=1989, maximum=2025)
        if not all(
            isinstance(value, UncertaintyBounds)
            for value in (self.state_based, self.non_state, self.one_sided, self.total)
        ):
            raise TypeError("country-year bounds must use UncertaintyBounds")
        expected = UncertaintyBounds(
            low=self.state_based.low + self.non_state.low + self.one_sided.low,
            best=self.state_based.best + self.non_state.best + self.one_sided.best,
            high=self.state_based.high + self.non_state.high + self.one_sided.high,
        )
        if self.total != expected:
            raise UCDPAggregateError("country-year total does not equal category sums")
        _digest(self.source_row_sha256, label="source_row_sha256")
        _digest(
            self.country_year_acquisition_id,
            label="country_year_acquisition_id",
        )
        if self.dataset_version != DATASET_VERSION:
            raise UCDPAggregateError("country-year dataset_version changed")
        if self.unit != "deaths" or self.evidence_state != "observed":
            raise UCDPAggregateError("country-year evidence semantics changed")
        if self.total_derivation != "sum_of_ucdp_category_bounds":
            raise UCDPAggregateError("country-year total derivation changed")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": COUNTRY_RECORD_SCHEMA_VERSION,
            "country_code": self.country_code,
            "year": self.year,
            "unit": self.unit,
            "evidence_state": self.evidence_state,
            "state_based": self.state_based.to_dict(),
            "non_state": self.non_state.to_dict(),
            "one_sided": self.one_sided.to_dict(),
            "total": self.total.to_dict(),
            "total_derivation": self.total_derivation,
            "dataset_version": self.dataset_version,
            "source_row_sha256": self.source_row_sha256,
            "country_year_acquisition_id": self.country_year_acquisition_id,
        }

    @property
    def record_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self._payload()))

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["record_id"] = self.record_id
        return payload


def _movement_lanes() -> list[dict[str, str]]:
    return [
        {
            "lane_id": "civic_society",
            "evidence_state": "unavailable",
            "ucdp_mapping": "none",
            "boundary": "UCDP armed-conflict data does not represent civic participation",
        },
        {
            "lane_id": "electoral_political",
            "evidence_state": "unavailable",
            "ucdp_mapping": "none",
            "boundary": "political parties and electoral positions require separate evidence",
        },
        {
            "lane_id": "armed_conflict_organizations",
            "evidence_state": "observed",
            "ucdp_mapping": "distinct_side_b_actor_ids_by_conflict_year",
            "boundary": "actor IDs remain separate and are not one unified movement",
        },
        {
            "lane_id": "state_authorities",
            "evidence_state": "observed",
            "ucdp_mapping": "distinct_side_a_actor_ids_by_conflict_year",
            "boundary": "source-side coding is not a finding about every attributed act",
        },
        {
            "lane_id": "human_rights_documentation",
            "evidence_state": "unavailable",
            "ucdp_mapping": "none",
            "boundary": "human-rights allegations and findings require separate evidence",
        },
    ]


@dataclass(frozen=True, slots=True)
class UCDPAggregateBundle:
    """Authenticated UCDP evidence reduced to the public annual allowlist."""

    generated_at: datetime
    latest_retrieved_at: datetime
    registry_sha256: str
    review_lock_sha256: str
    source: SourceRecord
    acquisition_receipts: tuple[AcquisitionReceiptRecord, ...]
    actor_registry_ids_sha256: str
    actor_registry_id_count: int
    conflict_years: tuple[ConflictYearAggregate, ...]
    country_years: tuple[CountryYearAggregate, ...]

    def __post_init__(self) -> None:
        generated_at = _utc(self.generated_at, label="generated_at")
        latest_retrieved_at = _utc(
            self.latest_retrieved_at,
            label="latest_retrieved_at",
        )
        _digest(self.registry_sha256, label="registry_sha256")
        _digest(self.review_lock_sha256, label="review_lock_sha256")
        _digest(
            self.actor_registry_ids_sha256,
            label="actor_registry_ids_sha256",
        )
        _integer(
            self.actor_registry_id_count,
            label="actor_registry_id_count",
            minimum=1,
            maximum=100_000,
        )
        if not isinstance(self.source, SourceRecord):
            raise TypeError("source must be a frozen SourceRecord")
        if self.source.review_lock_sha256 != self.review_lock_sha256:
            raise UCDPAggregateError("source has the wrong reviewed lock")
        if (
            type(self.acquisition_receipts) is not tuple
            or len(self.acquisition_receipts) != 3
        ):
            raise UCDPAggregateError("exactly three acquisitions are required")
        input_ids: set[str] = set()
        acquisition_ids: dict[str, str] = {}
        latest_retrieval: datetime | None = None
        for position, receipt in enumerate(self.acquisition_receipts, 1):
            if not isinstance(receipt, AcquisitionReceiptRecord):
                raise TypeError(
                    f"acquisition receipt {position} must be a frozen record"
                )
            input_id = receipt.input_id
            acquisition_id = receipt.acquisition_id
            if input_id in input_ids:
                raise UCDPAggregateError("acquisition input_id is duplicated")
            input_ids.add(input_id)
            acquisition_ids[input_id] = acquisition_id
            retrieved_at = parse_timestamp(receipt.retrieved_at, label="receipt retrieved_at")
            latest_retrieval = (
                max(latest_retrieval, retrieved_at)
                if latest_retrieval
                else retrieved_at
            )
        if input_ids != {
            "armed_conflict",
            "actor_registry",
            "organized_country_year",
        }:
            raise UCDPAggregateError("acquisition inputs are incomplete")
        if latest_retrieved_at != latest_retrieval:
            raise UCDPAggregateError(
                "latest_retrieved_at must equal the latest reviewed retrieval clock"
            )
        if generated_at < latest_retrieved_at:
            raise UCDPAggregateError("generated_at must not precede reviewed evidence")
        if type(self.conflict_years) is not tuple:
            raise TypeError("conflict_years must be a tuple")
        if type(self.country_years) is not tuple:
            raise TypeError("country_years must be a tuple")
        if not 1 <= len(self.conflict_years) <= MAX_CONFLICT_RECORDS:
            raise UCDPAggregateError("conflict-year coverage is empty or unbounded")
        if not 1 <= len(self.country_years) <= MAX_COUNTRY_RECORDS:
            raise UCDPAggregateError("country-year coverage is empty or unbounded")
        conflict_keys: set[tuple[str, int, int]] = set()
        for record in self.conflict_years:
            if not isinstance(record, ConflictYearAggregate):
                raise TypeError("conflict_years must contain ConflictYearAggregate")
            key = (record.geography_code, record.conflict_id, record.year)
            if key in conflict_keys:
                raise UCDPAggregateError("conflict-year identity is duplicated")
            conflict_keys.add(key)
            if (
                record.armed_conflict_acquisition_id
                != acquisition_ids["armed_conflict"]
            ):
                raise UCDPAggregateError("conflict row has the wrong acquisition")
            if (
                record.actor_registry_acquisition_id
                != acquisition_ids["actor_registry"]
            ):
                raise UCDPAggregateError("conflict row has the wrong actor registry")
        country_keys: set[tuple[str, int]] = set()
        for record in self.country_years:
            if not isinstance(record, CountryYearAggregate):
                raise TypeError("country_years must contain CountryYearAggregate")
            key = (record.country_code, record.year)
            if key in country_keys:
                raise UCDPAggregateError("country-year identity is duplicated")
            country_keys.add(key)
            if (
                record.country_year_acquisition_id
                != acquisition_ids["organized_country_year"]
            ):
                raise UCDPAggregateError("country-year row has the wrong acquisition")
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "latest_retrieved_at", latest_retrieved_at)

    def _payload(self) -> dict[str, object]:
        conflicts = [row.to_dict() for row in self.conflict_years]
        countries = [row.to_dict() for row in self.country_years]
        years = [row.year for row in (*self.conflict_years, *self.country_years)]
        payload: dict[str, object] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "generated_at": _timestamp(self.generated_at),
            "latest_retrieved_at": _timestamp(self.latest_retrieved_at),
            "scope_policy": {
                "geographies": ["MMR", "PAK-BAL"],
                "temporal_resolution": "annual_historical",
                "movement_scope": "plural_lanes_no_unified_movement_entity",
                "actor_identity": "ucdp_actor_ids_only_no_public_actor_names",
                "event_coordinates": "prohibited",
                "village_names": "prohibited",
                "event_narratives": "prohibited",
                "person_dossiers": "prohibited",
                "live_or_tactical_fields": "prohibited",
                "drug_actor_inference": "prohibited",
                "join_boundary": (
                    "historical_context_only_no_operational_or_causal_join"
                ),
                "missing_rights_stale_policy": "fail_closed_unavailable_not_zero",
            },
            "movement_lanes": _movement_lanes(),
            "source": self.source.to_dict(),
            "registry_sha256": self.registry_sha256,
            "review_lock_sha256": self.review_lock_sha256,
            "coverage": {
                "start_year": min(years),
                "end_year": max(years),
                "conflict_year_records": len(conflicts),
                "country_year_records": len(countries),
                "actor_registry_id_count": self.actor_registry_id_count,
            },
            "acquisition_receipts": [
                row.to_dict() for row in self.acquisition_receipts
            ],
            "actor_registry_ids_sha256": self.actor_registry_ids_sha256,
            "conflict_years_sha256": sha256_bytes(canonical_json_bytes(conflicts)),
            "country_years_sha256": sha256_bytes(canonical_json_bytes(countries)),
            "conflict_years": conflicts,
            "country_years": countries,
        }
        assert_public_safe(payload)
        return payload

    @property
    def bundle_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self._payload()))

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["bundle_id"] = self.bundle_id
        assert_public_safe(payload)
        return payload


def _require_closed_schema(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        if value.get("type") == "object" and value.get("additionalProperties") is not False:
            raise UCDPAggregateError(
                f"public schema object {path} is not recursively closed"
            )
        for key, nested in value.items():
            _require_closed_schema(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for position, nested in enumerate(value):
            _require_closed_schema(nested, path=f"{path}[{position}]")


def _record_content_id(value: Mapping[str, object], identity_key: str) -> str:
    payload = dict(value)
    identity = payload.pop(identity_key, None)
    _digest(identity, label=identity_key)
    expected = sha256_bytes(canonical_json_bytes(payload))
    if identity != expected:
        raise UCDPAggregateError(f"{identity_key} is not bound to exact record bytes")
    return expected


def _validate_public_semantics(document: Mapping[str, object]) -> None:
    _record_content_id(document, "bundle_id")
    review_lock = _digest(
        document.get("review_lock_sha256"),
        label="review_lock_sha256",
    )
    source = document.get("source")
    if type(source) is not dict or source.get("review_lock_sha256") != review_lock:
        raise UCDPAggregateError("public source is not bound to the reviewed lock")

    receipts = document.get("acquisition_receipts")
    if type(receipts) is not list or len(receipts) != 3:
        raise UCDPAggregateError("public acquisition coverage changed")
    acquisition_ids: dict[str, str] = {}
    for receipt in receipts:
        if type(receipt) is not dict:
            raise UCDPAggregateError("public acquisition receipt is not an object")
        acquisition_id = _record_content_id(receipt, "acquisition_id")
        input_id = receipt.get("input_id")
        if type(input_id) is not str or input_id in acquisition_ids:
            raise UCDPAggregateError("public acquisition input identity changed")
        acquisition_ids[input_id] = acquisition_id
    if set(acquisition_ids) != {
        "armed_conflict",
        "actor_registry",
        "organized_country_year",
    }:
        raise UCDPAggregateError("public acquisition inputs are incomplete")

    conflicts = document.get("conflict_years")
    countries = document.get("country_years")
    if type(conflicts) is not list or type(countries) is not list:
        raise UCDPAggregateError("public record arrays changed shape")
    if document.get("conflict_years_sha256") != sha256_bytes(
        canonical_json_bytes(conflicts)
    ):
        raise UCDPAggregateError("conflict-year array hash changed")
    if document.get("country_years_sha256") != sha256_bytes(
        canonical_json_bytes(countries)
    ):
        raise UCDPAggregateError("country-year array hash changed")

    conflict_keys: set[tuple[str, int, int]] = set()
    for row in conflicts:
        if type(row) is not dict:
            raise UCDPAggregateError("conflict-year row is not an object")
        _record_content_id(row, "record_id")
        geography = row.get("geography_code")
        country = row.get("country_code")
        expected_country = "PAK" if geography == "PAK-BAL" else "MMR"
        if geography not in GEOGRAPHIES or country != expected_country:
            raise UCDPAggregateError("conflict geography/country binding changed")
        year = _integer(row.get("year"), label="conflict year", minimum=1946)
        conflict_id = _integer(row.get("conflict_id"), label="conflict_id", minimum=1)
        key = (geography, conflict_id, year)
        if key in conflict_keys:
            raise UCDPAggregateError("conflict-year identity is duplicated")
        conflict_keys.add(key)
        side_a = _actor_ids(row.get("side_a_actor_ids"), label="side_a_actor_ids")
        side_b = _actor_ids(row.get("side_b_actor_ids"), label="side_b_actor_ids")
        if set(side_a) & set(side_b):
            raise UCDPAggregateError("public conflict actor sides overlap")
        if row.get("armed_conflict_acquisition_id") != acquisition_ids["armed_conflict"]:
            raise UCDPAggregateError("conflict row has the wrong acquisition")
        if row.get("actor_registry_acquisition_id") != acquisition_ids["actor_registry"]:
            raise UCDPAggregateError("conflict row has the wrong actor registry")

    country_keys: set[tuple[str, int]] = set()
    for row in countries:
        if type(row) is not dict:
            raise UCDPAggregateError("country-year row is not an object")
        _record_content_id(row, "record_id")
        country = row.get("country_code")
        if country not in COUNTRY_CODES:
            raise UCDPAggregateError("country-year country changed")
        year = _integer(row.get("year"), label="country-year year", minimum=1989)
        key = (country, year)
        if key in country_keys:
            raise UCDPAggregateError("country-year identity is duplicated")
        country_keys.add(key)
        bounds = []
        for name in ("state_based", "non_state", "one_sided", "total"):
            value = row.get(name)
            if type(value) is not dict:
                raise UCDPAggregateError(f"country-year {name} bounds changed")
            bounds.append(
                UncertaintyBounds(
                    low=value.get("low"),  # type: ignore[arg-type]
                    best=value.get("best"),  # type: ignore[arg-type]
                    high=value.get("high"),  # type: ignore[arg-type]
                )
            )
        expected_total = UncertaintyBounds(
            low=sum(value.low for value in bounds[:3]),
            best=sum(value.best for value in bounds[:3]),
            high=sum(value.high for value in bounds[:3]),
        )
        if bounds[3] != expected_total:
            raise UCDPAggregateError("country-year total does not equal category sums")
        if row.get("country_year_acquisition_id") != acquisition_ids[
            "organized_country_year"
        ]:
            raise UCDPAggregateError("country-year row has the wrong acquisition")


def canonical_public_bytes(
    bundle: UCDPAggregateBundle,
    *,
    schema_path: str | Path,
    forbidden_values: Sequence[str],
) -> bytes:
    """Construct, schema-check, semantically check, and scrub exact public bytes."""

    if not isinstance(bundle, UCDPAggregateBundle):
        raise TypeError("bundle must be UCDPAggregateBundle")
    document = bundle.to_dict()
    raw = canonical_json_bytes(document)
    try:
        reparsed = json.loads(raw.decode("utf-8", "strict"))
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UCDPAggregateError(f"cannot validate public UCDP bytes: {exc}") from exc
    if type(reparsed) is not dict or canonical_json_bytes(reparsed) != raw:
        raise UCDPAggregateError("public UCDP bytes are not exact canonical JSON")
    if type(schema) is not dict:
        raise UCDPAggregateError("public UCDP schema must be an object")
    _require_closed_schema(schema)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).validate(reparsed)
    except (SchemaError, ValidationError) as exc:
        raise UCDPAggregateError(f"public UCDP schema validation failed: {exc}") from exc
    _validate_public_semantics(reparsed)
    assert_public_safe(reparsed, forbidden_values=forbidden_values)
    return raw


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "AcquisitionReceiptRecord",
    "CitationRecord",
    "CONFLICT_RECORD_SCHEMA_VERSION",
    "COUNTRY_RECORD_SCHEMA_VERSION",
    "CountryYearAggregate",
    "DATASET_VERSION",
    "MYANMAR_TERRITORIES",
    "MAX_ACTOR_IDS_PER_SIDE",
    "PUBLICATION_SCOPE",
    "SourceRecord",
    "TRUST_MODEL",
    "ConflictYearAggregate",
    "UCDPAggregateBundle",
    "UCDPAggregateError",
    "UncertaintyBounds",
    "assert_public_safe",
    "canonical_public_bytes",
    "canonical_json_bytes",
    "parse_timestamp",
    "sha256_bytes",
]
