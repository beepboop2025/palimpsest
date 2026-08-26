"""Bounded World Bank WDI adapter for BRI-country national context.

The adapter makes one keyless request for China, Myanmar, and Pakistan.  Its
output is national economic context only: country-period co-movement cannot be
used to infer that BRI caused a value, that a project produced an outcome, or
that any person or organization is connected to a corridor.
"""

from __future__ import annotations

import json
import math
import re
import time as time_module
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from core.bri_observation import (
    BUNDLE_SCHEMA_VERSION,
    CAUSALITY_BOUNDARY,
    CONTEXT_SCOPE,
    RELEASE_TIME_SEMANTICS,
    BRIEconomicObservation,
    BRIRights,
    canonical_json_bytes,
    request_id_for,
    sha256_bytes,
)
from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import FetchError, safe_fetch_bytes


REGISTRY_SCHEMA_VERSION = "palimpsest.bri-wdi-series.v1"
PARSER_VERSION = "palimpsest-bri-world-bank-wdi-json.v1"
ACQUISITION_RECEIPT_SCHEMA_VERSION = "palimpsest.bri-wdi-acquisition-receipt.v1"
API_HOST = "api.worldbank.org"
API_PER_PAGE = 20_000
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_REGISTRY_BYTES = 512 * 1024
MAX_ROWS = 12_000
MAX_SERIES_PER_REQUEST = 24
MAX_COUNTRIES_PER_REQUEST = 3
MAX_YEARS_PER_REQUEST = 100
MAX_SOURCE_TEXT_BYTES = 4 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
USER_AGENT = (
    "palimpsest.info BRI observatory (World Bank WDI national context; "
    "contact desk@palimpsest.info)"
)

_INDICATOR_ID = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,79}$")
_SERIES_ID = re.compile(r"^bri\.context\.wdi\.[a-z0-9][a-z0-9_]{1,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOPICS = frozenset(
    {
        "macro",
        "trade",
        "finance",
        "labor",
        "energy",
        "environment",
        "logistics",
        "digital",
        "demographics",
    }
)
_METADATA_FIELDS = frozenset(
    {"page", "pages", "per_page", "total", "sourceid", "lastupdated"}
)
_ROW_FIELDS = frozenset(
    {
        "indicator",
        "country",
        "countryiso3code",
        "date",
        "value",
        "unit",
        "obs_status",
        "decimal",
        "footnote",
    }
)
_ROW_OPTIONAL_FIELDS = frozenset({"scale"})


class BRIWDIError(ValueError):
    """Registry, transport, or response bytes violated the reviewed contract."""


@dataclass(frozen=True, slots=True)
class CountryBinding:
    country_code: str
    api_country_id: str
    name: str


@dataclass(frozen=True, slots=True)
class SeriesBinding:
    indicator_id: str
    series_id: str
    source_title: str
    name: str
    unit: str
    topic: str


@dataclass(frozen=True, slots=True)
class WDIRegistry:
    dataset: Mapping[str, str]
    countries: Mapping[str, CountryBinding]
    bindings: Mapping[str, SeriesBinding]
    raw_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, Mapping):
            raise TypeError("registry dataset must be a mapping")
        if not isinstance(self.countries, Mapping) or not self.countries:
            raise TypeError("registry countries must be a non-empty mapping")
        if not isinstance(self.bindings, Mapping) or not self.bindings:
            raise TypeError("registry bindings must be a non-empty mapping")
        if type(self.raw_sha256) is not str or not _SHA256.fullmatch(self.raw_sha256):
            raise BRIWDIError("registry raw_sha256 must be a lowercase digest")
        countries = dict(self.countries)
        bindings = dict(self.bindings)
        if any(
            type(key) is not str
            or not isinstance(value, CountryBinding)
            or key != value.country_code
            for key, value in countries.items()
        ):
            raise BRIWDIError("registry country map is inconsistent")
        if any(
            type(key) is not str
            or not isinstance(value, SeriesBinding)
            or key != value.indicator_id
            for key, value in bindings.items()
        ):
            raise BRIWDIError("registry series map is inconsistent")
        object.__setattr__(self, "dataset", MappingProxyType(dict(self.dataset)))
        object.__setattr__(self, "countries", MappingProxyType(countries))
        object.__setattr__(self, "bindings", MappingProxyType(bindings))


@dataclass(frozen=True, slots=True)
class WDIRequestReceipt:
    acquisition_id: str
    request_id: str
    evidence_url: str
    raw_response_sha256: str
    response_bytes: int
    source_rows: int
    observed_rows: int
    forecast_rows: int
    unavailable_rows: int
    dataset_last_updated: date
    source_release_upper_bound: datetime
    retrieved_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "acquisition_id": self.acquisition_id,
            "request_id": self.request_id,
            "evidence_url": self.evidence_url,
            "raw_response_sha256": self.raw_response_sha256,
            "response_bytes": self.response_bytes,
            "source_rows": self.source_rows,
            "observed_rows": self.observed_rows,
            "forecast_rows": self.forecast_rows,
            "unavailable_rows": self.unavailable_rows,
            "dataset_last_updated": self.dataset_last_updated.isoformat(),
            "source_release_upper_bound": _timestamp(self.source_release_upper_bound),
            "retrieved_at": _timestamp(self.retrieved_at),
        }


@dataclass(frozen=True, slots=True)
class WDIAcquisitionReceipt:
    """Canonical sidecar authenticating immutable raw acquisition bytes."""

    request_id: str
    evidence_url: str
    raw_response_sha256: str
    response_bytes: int
    retrieved_at: datetime
    schema_version: str = ACQUISITION_RECEIPT_SCHEMA_VERSION
    request_method: str = "GET"
    request_user_agent: str = USER_AGENT
    redirect_policy: str = "disabled"
    tls_verification: str = "required"
    max_response_bytes: int = MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if self.schema_version != ACQUISITION_RECEIPT_SCHEMA_VERSION:
            raise BRIWDIError("unsupported acquisition receipt schema_version")
        _validate_fetch_url(self.evidence_url)
        if type(self.raw_response_sha256) is not str or not _SHA256.fullmatch(
            self.raw_response_sha256
        ):
            raise BRIWDIError("receipt raw_response_sha256 must be a lowercase digest")
        if (
            isinstance(self.response_bytes, bool)
            or not isinstance(self.response_bytes, int)
            or not 1 <= self.response_bytes <= MAX_RESPONSE_BYTES
        ):
            raise BRIWDIError("receipt response_bytes is outside the bounded response")
        if type(self.retrieved_at) is not datetime:
            raise TypeError("receipt retrieved_at must be a datetime")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise BRIWDIError("receipt retrieved_at must be timezone-aware")
        retrieved_at = self.retrieved_at.astimezone(UTC)
        if self.request_id != request_id_for(
            evidence_url=self.evidence_url,
            raw_response_sha256=self.raw_response_sha256,
        ):
            raise BRIWDIError("receipt request_id does not bind URL and raw bytes")
        fixed = {
            "request_method": (self.request_method, "GET"),
            "request_user_agent": (self.request_user_agent, USER_AGENT),
            "redirect_policy": (self.redirect_policy, "disabled"),
            "tls_verification": (self.tls_verification, "required"),
            "max_response_bytes": (self.max_response_bytes, MAX_RESPONSE_BYTES),
        }
        for name, (actual, expected) in fixed.items():
            if actual != expected:
                raise BRIWDIError(f"receipt {name} must be {expected!r}")
        object.__setattr__(self, "retrieved_at", retrieved_at)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "evidence_url": self.evidence_url,
            "request_method": self.request_method,
            "request_user_agent": self.request_user_agent,
            "redirect_policy": self.redirect_policy,
            "tls_verification": self.tls_verification,
            "max_response_bytes": self.max_response_bytes,
            "raw_response_sha256": self.raw_response_sha256,
            "response_bytes": self.response_bytes,
            "retrieved_at": _timestamp(self.retrieved_at),
        }

    @property
    def acquisition_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self._payload()))

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["acquisition_id"] = self.acquisition_id
        return payload

    @classmethod
    def from_bytes(cls, raw: bytes) -> "WDIAcquisitionReceipt":
        value = _strict_json_loads(
            raw,
            label="BRI WDI acquisition receipt",
            maximum_bytes=MAX_RECEIPT_BYTES,
        )
        expected_fields = {
            "schema_version",
            "acquisition_id",
            "request_id",
            "evidence_url",
            "request_method",
            "request_user_agent",
            "redirect_policy",
            "tls_verification",
            "max_response_bytes",
            "raw_response_sha256",
            "response_bytes",
            "retrieved_at",
        }
        if type(value) is not dict or set(value) != expected_fields:
            raise BRIWDIError("acquisition receipt fields changed")
        if canonical_json_bytes(value) != raw:
            raise BRIWDIError("acquisition receipt must use canonical JSON bytes")
        acquisition_id = value.pop("acquisition_id")
        if type(acquisition_id) is not str or not _SHA256.fullmatch(acquisition_id):
            raise BRIWDIError("acquisition_id must be a lowercase digest")
        value["retrieved_at"] = _parse_timestamp(
            value["retrieved_at"], label="receipt retrieved_at"
        )
        receipt = cls(**value)
        if receipt.acquisition_id != acquisition_id:
            raise BRIWDIError("acquisition_id does not authenticate the receipt")
        return receipt


@dataclass(frozen=True, slots=True)
class WDICollection:
    registry: WDIRegistry
    request_receipt: WDIRequestReceipt
    observations: tuple[BRIEconomicObservation, ...]
    requested_start_year: int
    requested_end_year: int

    def _payload(self) -> dict[str, object]:
        observation_rows = [row.to_dict() for row in self.observations]
        rights = BRIRights().to_dict()
        payload: dict[str, object] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "generated_at": _timestamp(self.request_receipt.retrieved_at),
            "context_policy": {
                "scope": CONTEXT_SCOPE,
                "aggregate_level": "country",
                "countries": sorted(self.registry.countries),
                "causality_boundary": CAUSALITY_BOUNDARY,
                "actor_inference": "prohibited",
                "project_attribution": "prohibited",
                "tactical_data": "prohibited",
                "missing_value_policy": "source_null_remains_unavailable",
                "forecast_policy": "source_obs_status_F_remains_forecast",
                "qualification_policy": (
                    "obs_status_footnote_scale_preserved_verbatim"
                ),
                "downstream_semantics": {
                    "observed": "numeric_source_value_without_forecast_marker",
                    "forecast": "numeric_source_value_marked_F_not_observed",
                    "unavailable": "source_null_not_zero_or_imputed",
                    "join_boundary": (
                        "country_period_context_only_no_project_actor_or_causal_join"
                    ),
                },
            },
            "source": {
                "source_id": self.registry.dataset["source_id"],
                "name": self.registry.dataset["name"],
                "publisher": self.registry.dataset["publisher"],
                "catalog_url": self.registry.dataset["catalog_url"],
                **rights,
                "indicator_provenance_boundary": self.registry.dataset[
                    "indicator_provenance_boundary"
                ],
            },
            "registry_sha256": self.registry.raw_sha256,
            "coverage": {
                "start_year": self.requested_start_year,
                "end_year": self.requested_end_year,
                "countries": len(self.registry.countries),
                "indicators": len(self.registry.bindings),
                "source_rows": self.request_receipt.source_rows,
                "observed_rows": self.request_receipt.observed_rows,
                "forecast_rows": self.request_receipt.forecast_rows,
                "unavailable_rows": self.request_receipt.unavailable_rows,
            },
            "request_receipts": [self.request_receipt.to_dict()],
            "observations_sha256": sha256_bytes(canonical_json_bytes(observation_rows)),
            "observations": observation_rows,
        }
        return payload

    @property
    def collection_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self._payload()))

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["collection_id"] = self.collection_id
        return payload


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise BRIWDIError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BRIWDIError(f"{label} must be a canonical UTC timestamp") from exc
    if _timestamp(parsed) != value:
        raise BRIWDIError(f"{label} must be a canonical UTC timestamp")
    return parsed


def _strict_json_loads(raw: bytes, *, label: str, maximum_bytes: int) -> Any:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise BRIWDIError(f"{label} is empty or exceeds {maximum_bytes} bytes")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise BRIWDIError(f"{label} is not strict UTF-8") from exc

    def reject_constant(value: str) -> None:
        raise BRIWDIError(f"{label} contains non-finite JSON number {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BRIWDIError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BRIWDIError(f"{label} is not valid JSON") from exc


def _required_text(
    value: object,
    *,
    path: str,
    maximum_bytes: int = MAX_SOURCE_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise BRIWDIError(f"{path} must be text")
    if not allow_empty and not value.strip():
        raise BRIWDIError(f"{path} must be non-empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise BRIWDIError(f"{path} exceeds {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise BRIWDIError(f"{path} contains control characters")
    return value


def _validate_https_url(value: object, *, path: str) -> str:
    url = _required_text(value, path=path, maximum_bytes=16 * 1024)
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BRIWDIError(f"{path} is not a valid URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise BRIWDIError(f"{path} must be a credential-free HTTPS URL")
    return url


def load_registry(path: str | Path) -> WDIRegistry:
    raw = Path(path).read_bytes()
    value = _strict_json_loads(
        raw, label="BRI WDI registry", maximum_bytes=MAX_REGISTRY_BYTES
    )
    if type(value) is not dict or set(value) != {
        "schema_version",
        "dataset",
        "countries",
        "series",
    }:
        raise BRIWDIError("BRI WDI registry fields changed")
    if value["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise BRIWDIError(f"registry must use {REGISTRY_SCHEMA_VERSION}")

    dataset = value["dataset"]
    dataset_fields = {
        "source_id",
        "source_number",
        "name",
        "publisher",
        "api_base",
        "catalog_url",
        "license",
        "license_url",
        "rights_evidence_url",
        "redistribution_status",
        "attribution",
        "release_time_semantics",
        "context_scope",
        "causality_boundary",
        "indicator_provenance_boundary",
    }
    if type(dataset) is not dict or set(dataset) != dataset_fields:
        raise BRIWDIError("BRI WDI dataset fields changed")
    expected_dataset = {
        "source_id": "world_bank_wdi",
        "source_number": "2",
        "name": "World Development Indicators",
        "publisher": "World Bank",
        "api_base": "https://api.worldbank.org/v2",
        "catalog_url": (
            "https://datacatalog.worldbank.org/search/dataset/0037712/"
            "world-development-indicators"
        ),
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "rights_evidence_url": (
            "https://datacatalog.worldbank.org/search/dataset/0037712/"
            "world-development-indicators"
        ),
        "redistribution_status": "allowed_with_attribution",
        "attribution": "World Bank, World Development Indicators",
        "release_time_semantics": RELEASE_TIME_SEMANTICS,
        "context_scope": CONTEXT_SCOPE,
        "causality_boundary": CAUSALITY_BOUNDARY,
    }
    for key, expected in expected_dataset.items():
        if dataset.get(key) != expected:
            raise BRIWDIError(f"dataset {key} must be {expected!r}")
    for key in dataset_fields:
        _required_text(dataset.get(key), path=f"dataset.{key}", maximum_bytes=16 * 1024)
    for key in ("api_base", "catalog_url", "license_url", "rights_evidence_url"):
        _validate_https_url(dataset[key], path=f"dataset.{key}")

    country_rows = value["countries"]
    if type(country_rows) is not list or len(country_rows) != MAX_COUNTRIES_PER_REQUEST:
        raise BRIWDIError("registry must contain exactly CHN, MMR, and PAK")
    expected_countries = {
        "CHN": ("CN", "China"),
        "MMR": ("MM", "Myanmar"),
        "PAK": ("PK", "Pakistan"),
    }
    countries: dict[str, CountryBinding] = {}
    for position, row in enumerate(country_rows, 1):
        if type(row) is not dict or set(row) != {
            "country_code",
            "api_country_id",
            "name",
        }:
            raise BRIWDIError(f"country binding {position} fields changed")
        country_code = row["country_code"]
        if country_code not in expected_countries or country_code in countries:
            raise BRIWDIError(f"country binding {position} is invalid or duplicated")
        api_country_id, name = expected_countries[country_code]
        if row["api_country_id"] != api_country_id or row["name"] != name:
            raise BRIWDIError(f"country binding {country_code} descriptor changed")
        countries[country_code] = CountryBinding(
            country_code=country_code,
            api_country_id=api_country_id,
            name=name,
        )
    if set(countries) != set(expected_countries):
        raise BRIWDIError("registry country coverage is incomplete")

    series_rows = value["series"]
    if (
        type(series_rows) is not list
        or not series_rows
        or len(series_rows) > MAX_SERIES_PER_REQUEST
    ):
        raise BRIWDIError(f"registry requires 1 to {MAX_SERIES_PER_REQUEST} series")
    bindings: dict[str, SeriesBinding] = {}
    seen_series_ids: set[str] = set()
    for position, row in enumerate(series_rows, 1):
        if type(row) is not dict or set(row) != {
            "indicator_id",
            "series_id",
            "source_title",
            "name",
            "unit",
            "topic",
        }:
            raise BRIWDIError(f"series binding {position} fields changed")
        indicator_id = row["indicator_id"]
        series_id = row["series_id"]
        if (
            type(indicator_id) is not str
            or not _INDICATOR_ID.fullmatch(indicator_id)
            or indicator_id in bindings
        ):
            raise BRIWDIError(f"series binding {position} indicator_id is invalid")
        if (
            type(series_id) is not str
            or not _SERIES_ID.fullmatch(series_id)
            or series_id in seen_series_ids
        ):
            raise BRIWDIError(f"series binding {position} series_id is invalid")
        for key in ("source_title", "name", "unit"):
            _required_text(row[key], path=f"series {indicator_id}.{key}")
        if row["topic"] not in _TOPICS:
            raise BRIWDIError(f"series {indicator_id} topic is invalid")
        bindings[indicator_id] = SeriesBinding(
            indicator_id=indicator_id,
            series_id=series_id,
            source_title=row["source_title"],
            name=row["name"],
            unit=row["unit"],
            topic=row["topic"],
        )
        seen_series_ids.add(series_id)
    return WDIRegistry(
        dataset=dict(dataset),
        countries=countries,
        bindings=bindings,
        raw_sha256=sha256_bytes(raw),
    )


def build_url(registry: WDIRegistry, *, start_year: int, end_year: int) -> str:
    if (
        isinstance(start_year, bool)
        or isinstance(end_year, bool)
        or not isinstance(start_year, int)
        or not isinstance(end_year, int)
        or not 1960 <= start_year <= end_year <= 2100
    ):
        raise BRIWDIError("year range must satisfy 1960 <= start <= end <= 2100")
    years = end_year - start_year + 1
    if years > MAX_YEARS_PER_REQUEST:
        raise BRIWDIError(f"request exceeds {MAX_YEARS_PER_REQUEST} annual periods")
    projected_rows = years * len(registry.countries) * len(registry.bindings)
    if projected_rows > MAX_ROWS:
        raise BRIWDIError(f"request projects more than {MAX_ROWS} rows")
    country_codes = ";".join(sorted(registry.countries))
    indicator_ids = ";".join(sorted(registry.bindings))
    query = urllib.parse.urlencode(
        {
            "source": registry.dataset["source_number"],
            "date": f"{start_year}:{end_year}",
            "format": "json",
            "per_page": API_PER_PAGE,
            "footnote": "y",
        }
    )
    return (
        f"{registry.dataset['api_base']}/country/{country_codes}/indicator/"
        f"{indicator_ids}?{query}"
    )


def _number(value: object, *, identity: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BRIWDIError(f"{identity} value must be numeric or null")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise BRIWDIError(f"{identity} value must be finite")
    return normalized


def _row_sha256(row: Mapping[str, Any]) -> str:
    normalized = dict(row)
    normalized.setdefault("scale", "")
    try:
        return sha256_bytes(canonical_json_bytes(normalized))
    except (TypeError, ValueError, RecursionError) as exc:
        raise BRIWDIError(f"source row cannot be canonicalized: {exc}") from exc


def acquisition_receipt_for(
    raw: bytes,
    *,
    evidence_url: str,
    retrieved_at: datetime,
) -> WDIAcquisitionReceipt:
    """Build the canonical transport sidecar for exact raw response bytes."""

    if type(raw) is not bytes or not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise BRIWDIError(
            f"raw response is empty or exceeds {MAX_RESPONSE_BYTES} bytes"
        )
    digest = sha256_bytes(raw)
    return WDIAcquisitionReceipt(
        request_id=request_id_for(
            evidence_url=evidence_url,
            raw_response_sha256=digest,
        ),
        evidence_url=evidence_url,
        raw_response_sha256=digest,
        response_bytes=len(raw),
        retrieved_at=retrieved_at,
    )


def verify_acquisition_receipt(
    receipt_bytes: bytes,
    *,
    raw: bytes,
    expected_url: str,
) -> WDIAcquisitionReceipt:
    """Verify canonical sidecar bytes against raw bytes and request scope."""

    receipt = WDIAcquisitionReceipt.from_bytes(receipt_bytes)
    if receipt.evidence_url != expected_url:
        raise BRIWDIError("acquisition receipt does not match the canonical request")
    if receipt.response_bytes != len(raw):
        raise BRIWDIError("acquisition receipt response_bytes does not match raw input")
    if receipt.raw_response_sha256 != sha256_bytes(raw):
        raise BRIWDIError("acquisition receipt hash does not match raw input")
    return receipt


def parse_response(
    raw: bytes,
    *,
    registry: WDIRegistry,
    evidence_url: str,
    start_year: int,
    end_year: int,
    retrieved_at: datetime,
) -> WDICollection:
    """Parse one exact response using an explicit post-retrieval knowledge clock."""

    if type(retrieved_at) is not datetime:
        raise TypeError("retrieved_at must be a datetime")
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise BRIWDIError("retrieved_at must be timezone-aware")
    retrieved_at = retrieved_at.astimezone(UTC)
    expected_url = build_url(registry, start_year=start_year, end_year=end_year)
    if evidence_url != expected_url:
        raise BRIWDIError("evidence_url must exactly match the canonical request")
    value = _strict_json_loads(
        raw, label="BRI WDI response", maximum_bytes=MAX_RESPONSE_BYTES
    )
    if type(value) is not list or len(value) != 2:
        raise BRIWDIError("response must contain exactly metadata and rows")
    metadata, rows = value
    if type(metadata) is not dict or set(metadata) != _METADATA_FIELDS:
        raise BRIWDIError("response metadata fields changed")
    if type(rows) is not list:
        raise BRIWDIError("response rows shape changed")
    if metadata["page"] != 1 or metadata["pages"] != 1:
        raise BRIWDIError("response is incomplete or unexpectedly paginated")
    expected_source_id = (
        registry.dataset["source_number"] if len(registry.bindings) == 1 else None
    )
    if (
        metadata["per_page"] != API_PER_PAGE
        or metadata["sourceid"] != expected_source_id
    ):
        raise BRIWDIError("response metadata does not match the request")
    total = metadata["total"]
    if isinstance(total, bool) or not isinstance(total, int) or total != len(rows):
        raise BRIWDIError("response total does not match row count")
    expected_total = (
        (end_year - start_year + 1) * len(registry.countries) * len(registry.bindings)
    )
    if total != expected_total:
        raise BRIWDIError(
            f"response matrix is incomplete: expected {expected_total}, received {total}"
        )
    if total > MAX_ROWS:
        raise BRIWDIError(f"response exceeds {MAX_ROWS} rows")
    try:
        dataset_last_updated = date.fromisoformat(str(metadata["lastupdated"]))
    except ValueError as exc:
        raise BRIWDIError("response lastupdated is not an ISO date") from exc
    if metadata["lastupdated"] != dataset_last_updated.isoformat():
        raise BRIWDIError("response lastupdated is not canonical")
    if dataset_last_updated > retrieved_at.date():
        raise BRIWDIError("response lastupdated is in the future")
    source_release_upper_bound = min(
        datetime.combine(dataset_last_updated, time(23, 59, 59), tzinfo=UTC),
        retrieved_at,
    )
    raw_response_sha256 = sha256_bytes(raw)
    request_id = request_id_for(
        evidence_url=evidence_url,
        raw_response_sha256=raw_response_sha256,
    )
    acquisition_receipt = acquisition_receipt_for(
        raw,
        evidence_url=evidence_url,
        retrieved_at=retrieved_at,
    )

    seen: set[tuple[str, str, int]] = set()
    observations: list[BRIEconomicObservation] = []
    observed_rows = 0
    forecast_rows = 0
    unavailable_rows = 0
    for position, row in enumerate(rows, 1):
        if type(row) is not dict or (
            set(row) != _ROW_FIELDS and set(row) != _ROW_FIELDS | _ROW_OPTIONAL_FIELDS
        ):
            raise BRIWDIError(f"row {position} fields changed")
        indicator = row["indicator"]
        if type(indicator) is not dict or set(indicator) != {"id", "value"}:
            raise BRIWDIError(f"row {position} indicator shape changed")
        indicator_id = indicator["id"]
        if indicator_id not in registry.bindings:
            raise BRIWDIError(f"row {position} contains an unrequested indicator")
        binding = registry.bindings[indicator_id]
        source_title = _required_text(
            indicator["value"], path=f"row {position} indicator title"
        )
        if source_title != binding.source_title:
            raise BRIWDIError(f"indicator {indicator_id} title changed")

        country_code = row["countryiso3code"]
        if country_code not in registry.countries:
            raise BRIWDIError(f"row {position} contains an unrequested country")
        country = registry.countries[country_code]
        descriptor = row["country"]
        if (
            type(descriptor) is not dict
            or set(descriptor) != {"id", "value"}
            or descriptor["id"] != country.api_country_id
            or descriptor["value"] != country.name
        ):
            raise BRIWDIError(f"row {position} country descriptor changed")

        year_text = row["date"]
        if type(year_text) is not str or not re.fullmatch(r"\d{4}", year_text):
            raise BRIWDIError(f"row {position} annual period is invalid")
        year = int(year_text)
        if not start_year <= year <= end_year:
            raise BRIWDIError(f"row {position} lies outside the requested years")
        identity = (country_code, indicator_id, year)
        if identity in seen:
            raise BRIWDIError(f"duplicate row {country_code} {indicator_id} {year}")
        seen.add(identity)

        _required_text(row["unit"], path=f"row {position} unit", allow_empty=True)
        scale = _required_text(
            row.get("scale", ""), path=f"row {position} scale", allow_empty=True
        )
        obs_status = _required_text(
            row["obs_status"],
            path=f"row {position} obs_status",
            allow_empty=True,
        )
        footnote = _required_text(
            row["footnote"],
            path=f"row {position} footnote",
            allow_empty=True,
        )
        if obs_status not in {"", "F"}:
            raise BRIWDIError(
                f"row {position} has unsupported nonempty obs_status {obs_status!r}"
            )
        decimal = row["decimal"]
        if (
            isinstance(decimal, bool)
            or not isinstance(decimal, int)
            or not 0 <= decimal <= 15
        ):
            raise BRIWDIError(f"row {position} decimal is invalid")

        if obs_status == "F":
            if row["value"] is None:
                raise BRIWDIError(
                    f"row {position} forecast obs_status requires a numeric value"
                )
            normalized_value = _number(
                row["value"],
                identity=f"{country_code} {indicator_id} {year}",
            )
            evidence_state = "forecast"
            unavailability_reason = None
            forecast_rows += 1
        elif row["value"] is None:
            normalized_value = None
            evidence_state = "unavailable"
            unavailability_reason = "source_value_null"
            unavailable_rows += 1
        else:
            normalized_value = _number(
                row["value"],
                identity=f"{country_code} {indicator_id} {year}",
            )
            evidence_state = "observed"
            unavailability_reason = None
            observed_rows += 1
        observations.append(
            BRIEconomicObservation(
                series_id=binding.series_id,
                indicator_id=indicator_id,
                country_code=country_code,
                value=normalized_value,
                unit=binding.unit,
                evidence_state=evidence_state,
                unavailability_reason=unavailability_reason,
                obs_status=obs_status,
                footnote=footnote,
                scale=scale,
                period_start=date(year, 1, 1),
                period_end=date(year, 12, 31),
                source_release_upper_bound=source_release_upper_bound,
                retrieved_at=retrieved_at,
                source_dataset_last_updated=dataset_last_updated,
                evidence_url=evidence_url,
                raw_response_sha256=raw_response_sha256,
                source_row_sha256=_row_sha256(row),
                request_id=request_id,
                acquisition_id=acquisition_receipt.acquisition_id,
            )
        )

    expected_identities = {
        (country_code, indicator_id, year)
        for country_code in registry.countries
        for indicator_id in registry.bindings
        for year in range(start_year, end_year + 1)
    }
    if seen != expected_identities:
        missing = sorted(expected_identities - seen)
        raise BRIWDIError(f"response matrix omits rows: {missing[:3]}")
    ordered = tuple(
        sorted(
            observations,
            key=lambda row: (
                row.series_id,
                row.country_code,
                row.period_start,
            ),
        )
    )
    receipt = WDIRequestReceipt(
        acquisition_id=acquisition_receipt.acquisition_id,
        request_id=request_id,
        evidence_url=evidence_url,
        raw_response_sha256=raw_response_sha256,
        response_bytes=len(raw),
        source_rows=len(rows),
        observed_rows=observed_rows,
        forecast_rows=forecast_rows,
        unavailable_rows=unavailable_rows,
        dataset_last_updated=dataset_last_updated,
        source_release_upper_bound=source_release_upper_bound,
        retrieved_at=retrieved_at,
    )
    return WDICollection(
        registry=registry,
        request_receipt=receipt,
        observations=ordered,
        requested_start_year=start_year,
        requested_end_year=end_year,
    )


def _validate_fetch_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BRIWDIError("fetch URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != API_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/v2/country/CHN;MMR;PAK/indicator/")
        or not parsed.query
    ):
        raise BRIWDIError(
            "fetch URL must use the reviewed World Bank three-country HTTPS scope"
        )


def fetch_bytes(
    url: str,
    *,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    timeout: float = 45.0,
    retries: int = 2,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> bytes:
    """Fetch bounded bytes with TLS verification and redirects disabled."""

    _validate_fetch_url(url)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise BRIWDIError("retries must be a non-negative integer")
    kill = kill_switch or KillSwitch()
    ceiling = rate_ceiling or RateCeiling(rate=0.2, capacity=1.0)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        kill.require_live()
        ceiling.acquire()
        try:
            raw = fetcher(
                url,
                max_bytes=MAX_RESPONSE_BYTES,
                timeout=timeout,
                max_redirects=0,
                headers={"User-Agent": USER_AGENT},
            )
            if type(raw) is not bytes or not raw:
                raise BRIWDIError("fetcher must return non-empty exact bytes")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise BRIWDIError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
            return raw
        except (FetchError, OSError, BRIWDIError) as exc:
            last_error = exc
            if attempt < retries:
                time_module.sleep(float(attempt + 1))
    raise BRIWDIError(f"fetch failed after {retries + 1} attempts: {last_error}")


def collect(
    registry: WDIRegistry,
    *,
    start_year: int,
    end_year: int,
    clock: Callable[[], datetime],
    fetch: Callable[[str], bytes] | None = None,
) -> WDICollection:
    """Fetch exact bytes and sample the required retrieval clock afterwards."""

    if not callable(clock):
        raise BRIWDIError("retrieval clock must be callable")
    url = build_url(registry, start_year=start_year, end_year=end_year)
    raw = fetch(url) if fetch is not None else fetch_bytes(url)
    if type(raw) is not bytes or not raw:
        raise BRIWDIError("fetcher must return non-empty exact bytes")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BRIWDIError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
    retrieved_at = clock()
    if type(retrieved_at) is not datetime:
        raise BRIWDIError("retrieval clock must return a datetime")
    return parse_response(
        raw,
        registry=registry,
        evidence_url=url,
        start_year=start_year,
        end_year=end_year,
        retrieved_at=retrieved_at,
    )


__all__ = [
    "ACQUISITION_RECEIPT_SCHEMA_VERSION",
    "API_PER_PAGE",
    "BRIWDIError",
    "CountryBinding",
    "MAX_RECEIPT_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_ROWS",
    "PARSER_VERSION",
    "REGISTRY_SCHEMA_VERSION",
    "SeriesBinding",
    "WDICollection",
    "WDIRegistry",
    "WDIRequestReceipt",
    "WDIAcquisitionReceipt",
    "acquisition_receipt_for",
    "build_url",
    "collect",
    "fetch_bytes",
    "load_registry",
    "parse_response",
    "verify_acquisition_receipt",
]
