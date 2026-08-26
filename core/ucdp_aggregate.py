"""Public, aggregate-only contract for reviewed UCDP annual bulk data.

This contract is intentionally narrower than the upstream datasets. It exposes
annual conflict/territory/actor identifiers and country-year uncertainty bounds.
It cannot carry event coordinates, village names, narratives, person records,
live tactical fields, or any inference connecting conflict actors to drugs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping

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
    }
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
    return normalized


def assert_public_safe(value: object) -> None:
    """Refuse prohibited fields at the final serialization boundary."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if type(key) is not str:
                raise UCDPAggregateError("public object keys must be text")
            if key.casefold() in _FORBIDDEN_PUBLIC_KEYS:
                raise UCDPAggregateError(f"prohibited public field: {key}")
            assert_public_safe(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            assert_public_safe(nested)


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
    registry_sha256: str
    source: Mapping[str, object]
    acquisition_receipts: tuple[Mapping[str, object], ...]
    actor_registry_ids_sha256: str
    actor_registry_id_count: int
    conflict_years: tuple[ConflictYearAggregate, ...]
    country_years: tuple[CountryYearAggregate, ...]

    def __post_init__(self) -> None:
        generated_at = _utc(self.generated_at, label="generated_at")
        _digest(self.registry_sha256, label="registry_sha256")
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
        if not isinstance(self.source, Mapping):
            raise TypeError("source must be a mapping")
        if (
            type(self.acquisition_receipts) is not tuple
            or len(self.acquisition_receipts) != 3
        ):
            raise UCDPAggregateError("exactly three acquisitions are required")
        input_ids: set[str] = set()
        acquisition_ids: dict[str, str] = {}
        latest_retrieval: datetime | None = None
        for position, receipt in enumerate(self.acquisition_receipts, 1):
            if not isinstance(receipt, Mapping):
                raise TypeError(f"acquisition receipt {position} must be a mapping")
            input_id = _text(receipt.get("input_id"), label="receipt input_id")
            acquisition_id = _digest(
                receipt.get("acquisition_id"), label="receipt acquisition_id"
            )
            if input_id in input_ids:
                raise UCDPAggregateError("acquisition input_id is duplicated")
            input_ids.add(input_id)
            acquisition_ids[input_id] = acquisition_id
            retrieved_at = parse_timestamp(
                receipt.get("retrieved_at"), label="receipt retrieved_at"
            )
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
        if generated_at != latest_retrieval:
            raise UCDPAggregateError(
                "generated_at must equal the latest authenticated retrieval clock"
            )
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

    def _payload(self) -> dict[str, object]:
        conflicts = [row.to_dict() for row in self.conflict_years]
        countries = [row.to_dict() for row in self.country_years]
        years = [row.year for row in (*self.conflict_years, *self.country_years)]
        payload: dict[str, object] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "generated_at": _timestamp(self.generated_at),
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
            "source": dict(self.source),
            "registry_sha256": self.registry_sha256,
            "coverage": {
                "start_year": min(years),
                "end_year": max(years),
                "conflict_year_records": len(conflicts),
                "country_year_records": len(countries),
                "actor_registry_id_count": self.actor_registry_id_count,
            },
            "acquisition_receipts": [dict(row) for row in self.acquisition_receipts],
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


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "CONFLICT_RECORD_SCHEMA_VERSION",
    "COUNTRY_RECORD_SCHEMA_VERSION",
    "CountryYearAggregate",
    "DATASET_VERSION",
    "MYANMAR_TERRITORIES",
    "ConflictYearAggregate",
    "UCDPAggregateBundle",
    "UCDPAggregateError",
    "UncertaintyBounds",
    "assert_public_safe",
    "canonical_json_bytes",
    "parse_timestamp",
    "sha256_bytes",
]
