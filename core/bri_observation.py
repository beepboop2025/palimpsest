"""Strict aggregate observation contract for BRI-country economic context.

This module intentionally does *not* model BRI projects, organizations, people,
routes, or causal effects.  It carries national annual context for China,
Pakistan, and Myanmar with separate economic-period, source-release-upper-bound,
and retrieval clocks.  Missing source values remain explicit unavailable
observations; they are never converted to numeric zeroes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from numbers import Real
from typing import Any, Mapping
from urllib.parse import urlsplit


OBSERVATION_SCHEMA_VERSION = "palimpsest.bri-economic-observation.v1"
BUNDLE_SCHEMA_VERSION = "palimpsest.bri-economic-observations.v1"
COUNTRY_CODES = frozenset({"CHN", "MMR", "PAK"})
EVIDENCE_STATES = frozenset({"observed", "unavailable"})
CONTEXT_SCOPE = "national_economic_context"
CAUSALITY_BOUNDARY = "not_evidence_of_bri_causality"
RELEASE_TIME_SEMANTICS = "dataset_lastupdated_upper_bound"
AGGREGATE_LEVEL = "country"
FREQUENCY = "A"
SOURCE_ID = "world_bank_wdi"
PUBLISHER = "World Bank"

_SERIES_ID = re.compile(r"^bri\.context\.wdi\.[a-z0-9][a-z0-9_]{1,119}$")
_INDICATOR_ID = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BRIObservationError(ValueError):
    """A BRI economic observation violated the public contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical UTF-8 JSON representation used by all IDs."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    if type(value) is not bytes:
        raise TypeError("sha256 input must be bytes")
    return hashlib.sha256(value).hexdigest()


def _text(value: object, name: str, *, maximum_bytes: int = 512) -> str:
    if type(value) is not str or not value.strip():
        raise BRIObservationError(f"{name} must be non-empty text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise BRIObservationError(f"{name} exceeds {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise BRIObservationError(f"{name} contains control characters")
    return value


def _digest(value: object, name: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise BRIObservationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise BRIObservationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise BRIObservationError(f"{name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BRIObservationError(f"{name} must be a canonical UTC timestamp") from exc
    if _timestamp(parsed) != value:
        raise BRIObservationError(f"{name} must be a canonical UTC timestamp")
    return parsed


def _calendar_date(value: object, name: str) -> date:
    if type(value) is not date:
        raise TypeError(f"{name} must be a date (not datetime)")
    return value


def _parse_date(value: object, name: str) -> date:
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise BRIObservationError(f"{name} must be an ISO calendar date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise BRIObservationError(f"{name} must be an ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise BRIObservationError(f"{name} must be an ISO calendar date")
    return parsed


def _https_world_bank_url(value: object) -> str:
    url = _text(value, "evidence_url", maximum_bytes=16 * 1024)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BRIObservationError("evidence_url is not a valid URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.worldbank.org"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/v2/country/CHN;MMR;PAK/indicator/")
        or not parsed.query
    ):
        raise BRIObservationError(
            "evidence_url must use the reviewed World Bank three-country HTTPS scope"
        )
    return url


def request_id_for(*, evidence_url: str, raw_response_sha256: str) -> str:
    """Bind a transport receipt to its canonical URL and exact response bytes."""

    url = _https_world_bank_url(evidence_url)
    digest = _digest(raw_response_sha256, "raw_response_sha256")
    return sha256_bytes(
        canonical_json_bytes({"evidence_url": url, "raw_response_sha256": digest})
    )


@dataclass(frozen=True, slots=True)
class BRIRights:
    """Reviewed dataset-level reuse terms that travel with every observation."""

    license: str = "CC-BY-4.0"
    license_url: str = "https://creativecommons.org/licenses/by/4.0/"
    attribution: str = "World Bank, World Development Indicators"
    redistribution_status: str = "allowed_with_attribution"
    rights_evidence_url: str = (
        "https://datacatalog.worldbank.org/search/dataset/0037712/"
        "world-development-indicators"
    )

    def __post_init__(self) -> None:
        if (
            self.license != "CC-BY-4.0"
            or self.license_url != "https://creativecommons.org/licenses/by/4.0/"
            or self.attribution != "World Bank, World Development Indicators"
            or self.redistribution_status != "allowed_with_attribution"
            or self.rights_evidence_url
            != (
                "https://datacatalog.worldbank.org/search/dataset/0037712/"
                "world-development-indicators"
            )
        ):
            raise BRIObservationError("rights must match the reviewed WDI attribution")

    def to_dict(self) -> dict[str, str]:
        return {
            "license": self.license,
            "license_url": self.license_url,
            "attribution": self.attribution,
            "redistribution_status": self.redistribution_status,
            "rights_evidence_url": self.rights_evidence_url,
        }


@dataclass(frozen=True, slots=True)
class BRIEconomicObservation:
    """One country-level annual value or explicit unavailable source row.

    ``period_start``/``period_end`` are economic valid time.  The source only
    publishes a dataset-wide ``lastupdated`` date, so
    ``source_release_upper_bound`` is conservatively capped at the retrieval
    clock.  ``retrieved_at`` is Palimpsest knowledge time.
    """

    series_id: str
    indicator_id: str
    country_code: str
    value: float | None
    unit: str
    evidence_state: str
    unavailability_reason: str | None
    period_start: date
    period_end: date
    source_release_upper_bound: datetime
    retrieved_at: datetime
    source_dataset_last_updated: date
    evidence_url: str
    raw_response_sha256: str
    source_row_sha256: str
    request_id: str
    schema_version: str = OBSERVATION_SCHEMA_VERSION
    frequency: str = FREQUENCY
    aggregate_level: str = AGGREGATE_LEVEL
    source_id: str = SOURCE_ID
    publisher: str = PUBLISHER
    context_scope: str = CONTEXT_SCOPE
    causality_boundary: str = CAUSALITY_BOUNDARY
    release_time_semantics: str = RELEASE_TIME_SEMANTICS
    rights: BRIRights = field(default_factory=BRIRights)

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise BRIObservationError("unsupported observation schema_version")
        if type(self.series_id) is not str or not _SERIES_ID.fullmatch(self.series_id):
            raise BRIObservationError("series_id is not a BRI WDI context series")
        if type(self.indicator_id) is not str or not _INDICATOR_ID.fullmatch(
            self.indicator_id
        ):
            raise BRIObservationError("indicator_id is invalid")
        if self.country_code not in COUNTRY_CODES:
            raise BRIObservationError("country_code must be CHN, MMR, or PAK")
        _text(self.unit, "unit")
        if self.evidence_state not in EVIDENCE_STATES:
            raise BRIObservationError("evidence_state must be observed or unavailable")

        if self.evidence_state == "observed":
            if isinstance(self.value, bool) or not isinstance(self.value, Real):
                raise BRIObservationError("observed value must be a real number")
            normalized_value = float(self.value)
            if not math.isfinite(normalized_value):
                raise BRIObservationError("observed value must be finite")
            if self.unavailability_reason is not None:
                raise BRIObservationError(
                    "observed rows cannot carry an unavailability_reason"
                )
        else:
            if self.value is not None:
                raise BRIObservationError("unavailable rows must retain a null value")
            if self.unavailability_reason != "source_value_null":
                raise BRIObservationError(
                    "unavailable rows must state source_value_null"
                )
            normalized_value = None

        period_start = _calendar_date(self.period_start, "period_start")
        period_end = _calendar_date(self.period_end, "period_end")
        if period_start != date(period_start.year, 1, 1) or period_end != date(
            period_start.year, 12, 31
        ):
            raise BRIObservationError(
                "BRI WDI observations must cover one calendar year"
            )

        dataset_date = _calendar_date(
            self.source_dataset_last_updated, "source_dataset_last_updated"
        )
        retrieved_at = _utc(self.retrieved_at, "retrieved_at")
        release_upper_bound = _utc(
            self.source_release_upper_bound, "source_release_upper_bound"
        )
        if dataset_date > retrieved_at.date():
            raise BRIObservationError("source_dataset_last_updated is in the future")
        expected_upper_bound = min(
            datetime.combine(dataset_date, time(23, 59, 59), tzinfo=UTC),
            retrieved_at,
        )
        if release_upper_bound != expected_upper_bound:
            raise BRIObservationError(
                "source_release_upper_bound must conservatively bind lastupdated to retrieval"
            )

        evidence_url = _https_world_bank_url(self.evidence_url)
        raw_response_sha256 = _digest(self.raw_response_sha256, "raw_response_sha256")
        _digest(self.source_row_sha256, "source_row_sha256")
        request_id = _digest(self.request_id, "request_id")
        if request_id != request_id_for(
            evidence_url=evidence_url,
            raw_response_sha256=raw_response_sha256,
        ):
            raise BRIObservationError("request_id does not bind the URL and response")
        if not isinstance(self.rights, BRIRights):
            raise TypeError("rights must be BRIRights")

        fixed = {
            "frequency": (self.frequency, FREQUENCY),
            "aggregate_level": (self.aggregate_level, AGGREGATE_LEVEL),
            "source_id": (self.source_id, SOURCE_ID),
            "publisher": (self.publisher, PUBLISHER),
            "context_scope": (self.context_scope, CONTEXT_SCOPE),
            "causality_boundary": (self.causality_boundary, CAUSALITY_BOUNDARY),
            "release_time_semantics": (
                self.release_time_semantics,
                RELEASE_TIME_SEMANTICS,
            ),
        }
        for name, (actual, expected) in fixed.items():
            if actual != expected:
                raise BRIObservationError(f"{name} must be {expected!r}")

        object.__setattr__(self, "value", normalized_value)
        object.__setattr__(self, "retrieved_at", retrieved_at)
        object.__setattr__(self, "source_release_upper_bound", release_upper_bound)
        object.__setattr__(self, "evidence_url", evidence_url)

    @property
    def natural_key(self) -> tuple[str, str, date, date]:
        return (
            self.series_id,
            self.country_code,
            self.period_start,
            self.period_end,
        )

    def _record_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "series_id": self.series_id,
            "indicator_id": self.indicator_id,
            "country_code": self.country_code,
            "value": self.value,
            "unit": self.unit,
            "evidence_state": self.evidence_state,
            "unavailability_reason": self.unavailability_reason,
            "frequency": self.frequency,
            "aggregate_level": self.aggregate_level,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "source_release_upper_bound": _timestamp(self.source_release_upper_bound),
            "retrieved_at": _timestamp(self.retrieved_at),
            "source_dataset_last_updated": self.source_dataset_last_updated.isoformat(),
            "source_id": self.source_id,
            "publisher": self.publisher,
            "evidence_url": self.evidence_url,
            "raw_response_sha256": self.raw_response_sha256,
            "source_row_sha256": self.source_row_sha256,
            "request_id": self.request_id,
            "context_scope": self.context_scope,
            "causality_boundary": self.causality_boundary,
            "release_time_semantics": self.release_time_semantics,
            "rights": self.rights.to_dict(),
        }

    @property
    def observation_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self._record_payload()))

    def to_dict(self) -> dict[str, Any]:
        payload = self._record_payload()
        payload["observation_id"] = self.observation_id
        return payload

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "BRIEconomicObservation":
        if not isinstance(row, Mapping):
            raise TypeError("observation must be a mapping")
        expected_fields = {
            "schema_version",
            "series_id",
            "indicator_id",
            "country_code",
            "value",
            "unit",
            "evidence_state",
            "unavailability_reason",
            "frequency",
            "aggregate_level",
            "period_start",
            "period_end",
            "source_release_upper_bound",
            "retrieved_at",
            "source_dataset_last_updated",
            "source_id",
            "publisher",
            "evidence_url",
            "raw_response_sha256",
            "source_row_sha256",
            "request_id",
            "context_scope",
            "causality_boundary",
            "release_time_semantics",
            "rights",
            "observation_id",
        }
        if set(row) != expected_fields:
            raise BRIObservationError("observation fields changed")
        supplied_id = _digest(row["observation_id"], "observation_id")
        rights_value = row["rights"]
        if not isinstance(rights_value, Mapping) or set(rights_value) != {
            "license",
            "license_url",
            "attribution",
            "redistribution_status",
            "rights_evidence_url",
        }:
            raise BRIObservationError("rights fields changed")
        data = dict(row)
        data.pop("observation_id")
        data["period_start"] = _parse_date(data["period_start"], "period_start")
        data["period_end"] = _parse_date(data["period_end"], "period_end")
        data["source_dataset_last_updated"] = _parse_date(
            data["source_dataset_last_updated"], "source_dataset_last_updated"
        )
        data["source_release_upper_bound"] = _parse_timestamp(
            data["source_release_upper_bound"], "source_release_upper_bound"
        )
        data["retrieved_at"] = _parse_timestamp(data["retrieved_at"], "retrieved_at")
        data["rights"] = BRIRights(**dict(rights_value))
        observation = cls(**data)
        if observation.observation_id != supplied_id:
            raise BRIObservationError("observation_id does not authenticate the record")
        return observation


__all__ = [
    "AGGREGATE_LEVEL",
    "BRIEconomicObservation",
    "BRIObservationError",
    "BRIRights",
    "BUNDLE_SCHEMA_VERSION",
    "CAUSALITY_BOUNDARY",
    "CONTEXT_SCOPE",
    "COUNTRY_CODES",
    "OBSERVATION_SCHEMA_VERSION",
    "RELEASE_TIME_SEMANTICS",
    "canonical_json_bytes",
    "request_id_for",
    "sha256_bytes",
]
