"""World Bank WDI transport for broad, licensed China structural history.

The World Development Indicators dataset is a useful bootstrap for the China
economic observatory because the transport is keyless and the dataset catalog
declares CC BY 4.0.  It is deliberately treated as a *transport*, not as an
independent confirmation of every upstream series: many WDI rows originate in
Chinese official statistics or other international organizations.

WDI does not expose a per-observation publication timestamp.  Each imported
row therefore receives the earlier of the response's dataset-wide
``lastupdated`` end-of-day and Palimpsest's actual collection time.  This is a
conservative upper bound on knowability, so a historical backfill never becomes
fake real-time evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as daytime
from pathlib import Path
from typing import Any, Callable, Mapping

from core.econ_observation import EconomicObservation
from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import FetchError, safe_fetch_bytes


PARSER_VERSION = "world-bank-wdi-json.v1"
REGISTRY_SCHEMA = "palimpsest-china-econ-wdi-series.v1"
API_HOST = "api.worldbank.org"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SERIES_PER_REQUEST = 60
MAX_ROWS = 100_000
MAX_SOURCE_TITLE_BYTES = 512
MAX_FOOTNOTE_BYTES = 4096
MAX_PROVENANCE_RECEIPT_BYTES = 8 * 1024 * 1024
# The API rejects per_page=100000 even though our local parser can safely hold
# that many rows.  Its documented service accepts 20,000, which still covers
# 60 annual series over the complete WDI history in one atomic response.
API_PER_PAGE = 20_000
USER_AGENT = (
    "palimpsest.info observatory (World Bank WDI China aggregate ingest; "
    "contact desk@palimpsest.info)"
)
_IDENTIFIER = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,79}$")
_SERIES_ID = re.compile(r"^cn\.wdi\.[a-z0-9][a-z0-9_]{1,119}$")
_DOMAINS = frozenset(
    {
        "activity",
        "firm_health",
        "investment",
        "credit",
        "inflation",
        "labor",
        "property",
        "trade",
        "consumer_digital",
        "commodities",
        "agriculture",
        "logistics",
    }
)
_MARKET_CHANNELS = frozenset({"money_market", "capital_market"})
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


class WDIError(ValueError):
    """The WDI registry or response failed a reviewed contract."""


@dataclass(frozen=True, slots=True)
class WDISeriesBinding:
    indicator_id: str
    series_id: str
    name: str
    unit: str
    domain: str
    market_channels: tuple[str, ...]
    quality: float


@dataclass(frozen=True, slots=True)
class WDIRegistry:
    dataset: Mapping[str, str]
    bindings: Mapping[str, WDISeriesBinding]


@dataclass(frozen=True, slots=True)
class WDIResponse:
    observations: tuple[EconomicObservation, ...]
    raw_sha256: str
    evidence_url: str
    dataset_last_updated: date
    source_rows: int
    null_rows: int
    requested_start_year: int
    requested_end_year: int
    represented_indicators: tuple[str, ...]
    populated_indicators: tuple[str, ...]
    indicator_provenance: tuple["WDIIndicatorProvenance", ...]
    availability: tuple["WDIAvailability", ...]


@dataclass(frozen=True, slots=True)
class WDIIndicatorProvenance:
    indicator_id: str
    source_title: str
    reviewed_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "indicator_id": self.indicator_id,
            "source_title": self.source_title,
            "reviewed_name": self.reviewed_name,
        }


@dataclass(frozen=True, slots=True)
class WDIAvailability:
    indicator_id: str
    year: int
    available: bool
    footnote: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "indicator_id": self.indicator_id,
            "year": self.year,
            "available": self.available,
            "footnote": self.footnote,
        }


def _strict_json_loads(raw: bytes, *, label: str) -> Any:
    if type(raw) is not bytes or not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise WDIError(f"{label} is empty or exceeds {MAX_RESPONSE_BYTES} bytes")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise WDIError(f"{label} is not strict UTF-8") from exc

    def reject_constant(value: str) -> None:
        raise WDIError(f"{label} contains non-finite JSON number {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise WDIError(f"{label} contains duplicate key {key!r}")
            out[key] = value
        return out

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise WDIError(f"{label} is not valid JSON") from exc


def load_registry(path: str | Path) -> WDIRegistry:
    raw = Path(path).read_bytes()
    value = _strict_json_loads(raw, label="WDI series registry")
    if type(value) is not dict or value.get("schema_version") != REGISTRY_SCHEMA:
        raise WDIError(f"WDI registry must use {REGISTRY_SCHEMA}")
    dataset = value.get("dataset")
    series = value.get("series")
    if type(dataset) is not dict or type(series) is not list or not series:
        raise WDIError("WDI registry requires dataset metadata and a non-empty series list")
    required_dataset = {
        "source_id": "world_bank_wdi",
        "source_number": "2",
        "country_code": "CHN",
        "api_base": "https://api.worldbank.org/v2",
        "license": "CC-BY-4.0",
        "redistribution_status": "allowed",
        "release_time_semantics": "dataset_lastupdated_upper_bound",
        "per_indicator_upstream_metadata_status": "residual_gate",
    }
    for key, expected in required_dataset.items():
        if dataset.get(key) != expected:
            raise WDIError(f"WDI dataset {key} must be {expected!r}")
    for key in (
        "name",
        "publisher",
        "catalog_url",
        "license_url",
        "rights_evidence_url",
        "independence_group",
        "attribution",
        "per_indicator_upstream_metadata_requirement",
    ):
        if type(dataset.get(key)) is not str or not dataset[key].strip():
            raise WDIError(f"WDI dataset {key} is required")
    for key in ("catalog_url", "license_url", "rights_evidence_url"):
        parsed = urllib.parse.urlsplit(dataset[key])
        if parsed.scheme != "https" or not parsed.hostname:
            raise WDIError(f"WDI dataset {key} must be an HTTPS URL")
    if len(series) > MAX_SERIES_PER_REQUEST:
        raise WDIError(f"WDI registry exceeds {MAX_SERIES_PER_REQUEST} series")

    bindings: dict[str, WDISeriesBinding] = {}
    seen_series: set[str] = set()
    for position, row in enumerate(series, 1):
        if type(row) is not dict:
            raise WDIError(f"WDI series {position} must be an object")
        indicator_id = row.get("indicator_id")
        series_id = row.get("series_id")
        if type(indicator_id) is not str or not _IDENTIFIER.fullmatch(indicator_id):
            raise WDIError(f"WDI series {position} has an invalid indicator_id")
        if indicator_id in bindings:
            raise WDIError(f"duplicate WDI indicator_id {indicator_id}")
        if type(series_id) is not str or not _SERIES_ID.fullmatch(series_id):
            raise WDIError(f"WDI series {position} has an invalid series_id")
        if series_id in seen_series:
            raise WDIError(f"duplicate WDI series_id {series_id}")
        name, unit = row.get("name"), row.get("unit")
        if not all(type(item) is str and item.strip() for item in (name, unit)):
            raise WDIError(f"WDI series {indicator_id} requires name and unit")
        domain = row.get("domain")
        if domain not in _DOMAINS:
            raise WDIError(f"WDI series {indicator_id} has invalid domain")
        channels = row.get("market_channels")
        if (
            type(channels) is not list
            or not channels
            or any(channel not in _MARKET_CHANNELS for channel in channels)
            or len(set(channels)) != len(channels)
        ):
            raise WDIError(f"WDI series {indicator_id} has invalid market_channels")
        quality = row.get("quality")
        if isinstance(quality, bool) or not isinstance(quality, (int, float)):
            raise WDIError(f"WDI series {indicator_id} quality must be numeric")
        normalized_quality = float(quality)
        if not math.isfinite(normalized_quality) or not 0 <= normalized_quality <= 1:
            raise WDIError(f"WDI series {indicator_id} quality must lie in [0, 1]")
        bindings[indicator_id] = WDISeriesBinding(
            indicator_id=indicator_id,
            series_id=series_id,
            name=name,
            unit=unit,
            domain=domain,
            market_channels=tuple(channels),
            quality=normalized_quality,
        )
        seen_series.add(series_id)
    return WDIRegistry(dataset=dict(dataset), bindings=bindings)


def build_url(registry: WDIRegistry, *, start_year: int, end_year: int) -> str:
    if not 1960 <= start_year <= end_year <= 2100:
        raise WDIError("WDI year range must satisfy 1960 <= start <= end <= 2100")
    codes = ";".join(sorted(registry.bindings))
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
        f"{registry.dataset['api_base']}/country/"
        f"{registry.dataset['country_code']}/indicator/{codes}?{query}"
    )


def _number(value: object, *, indicator_id: str, year: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WDIError(f"WDI {indicator_id} {year} value must be numeric or null")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise WDIError(f"WDI {indicator_id} {year} value must be finite")
    return parsed


def _bounded_source_text(
    value: object,
    *,
    path: str,
    maximum_bytes: int,
    allow_empty: bool,
) -> str:
    if type(value) is not str:
        raise WDIError(f"{path} must be a string")
    if not allow_empty and not value.strip():
        raise WDIError(f"{path} must be non-empty")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise WDIError(f"{path} exceeds {maximum_bytes} UTF-8 bytes")
    return value


def _row_fingerprint(row: Mapping[str, Any]) -> str:
    """Hash one canonical source row, independent of batch encoding and order."""

    normalized = dict(row)
    normalized.setdefault("scale", "")
    try:
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise WDIError(f"WDI row cannot be fingerprinted: {exc}") from exc
    return hashlib.sha256(payload).hexdigest()


def parse_response(
    raw: bytes,
    *,
    registry: WDIRegistry,
    evidence_url: str,
    start_year: int,
    end_year: int,
    collected_at: datetime,
) -> WDIResponse:
    """Validate one complete WDI response and produce aggregate observations."""

    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise WDIError("WDI collected_at must be timezone-aware")
    collected_at = collected_at.astimezone(UTC)
    expected_url = build_url(registry, start_year=start_year, end_year=end_year)
    if evidence_url != expected_url:
        raise WDIError("WDI evidence_url must exactly match the canonical request scope")
    value = _strict_json_loads(raw, label="WDI response")
    if type(value) is not list or len(value) != 2:
        raise WDIError("WDI response must contain metadata and rows")
    metadata, rows = value
    if type(metadata) is not dict or type(rows) is not list:
        raise WDIError("WDI response metadata/rows shape changed")
    if set(metadata) != _METADATA_FIELDS:
        raise WDIError("WDI response metadata fields changed")
    if metadata.get("page") != 1 or metadata.get("pages") != 1:
        raise WDIError("WDI response is incomplete or unexpectedly paginated")
    expected_source_id = (
        registry.dataset["source_number"] if len(registry.bindings) == 1 else None
    )
    if (
        metadata.get("per_page") != API_PER_PAGE
        or metadata.get("sourceid") != expected_source_id
    ):
        raise WDIError("WDI response metadata does not match the canonical request")
    total = metadata.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total != len(rows):
        raise WDIError("WDI response total does not match row count")
    if total > MAX_ROWS:
        raise WDIError(f"WDI response exceeds {MAX_ROWS} rows")
    try:
        last_updated = date.fromisoformat(str(metadata["lastupdated"]))
    except (KeyError, ValueError) as exc:
        raise WDIError("WDI response lacks a valid lastupdated date") from exc
    if last_updated > collected_at.date():
        raise WDIError("WDI response lastupdated is in the future")
    dataset_release_upper_bound = datetime.combine(
        last_updated,
        daytime(23, 59, 59),
        tzinfo=UTC,
    )
    released_at = min(dataset_release_upper_bound, collected_at)
    raw_sha256 = hashlib.sha256(raw).hexdigest()

    observations: list[EconomicObservation] = []
    null_rows = 0
    seen: set[tuple[str, int]] = set()
    represented: set[str] = set()
    populated: set[str] = set()
    source_titles: dict[str, str] = {}
    availability: list[WDIAvailability] = []
    for position, row in enumerate(rows, 1):
        if type(row) is not dict or (
            set(row) != _ROW_FIELDS
            and set(row) != _ROW_FIELDS | _ROW_OPTIONAL_FIELDS
        ):
            raise WDIError(f"WDI row {position} fields changed")
        indicator = row.get("indicator")
        if type(indicator) is not dict or set(indicator) != {"id", "value"}:
            raise WDIError(f"WDI row {position} indicator shape changed")
        indicator_id = indicator.get("id")
        if indicator_id not in registry.bindings:
            raise WDIError(f"WDI row {position} contains an unrequested indicator")
        source_title = _bounded_source_text(
            indicator.get("value"),
            path=f"WDI row {position} indicator title",
            maximum_bytes=MAX_SOURCE_TITLE_BYTES,
            allow_empty=False,
        )
        previous_title = source_titles.setdefault(indicator_id, source_title)
        if previous_title != source_title:
            raise WDIError(f"WDI indicator {indicator_id} has inconsistent titles")
        country = row.get("country")
        if (
            type(country) is not dict
            or set(country) != {"id", "value"}
            or country.get("id") != "CN"
            or country.get("value") != "China"
        ):
            raise WDIError(f"WDI row {position} has an invalid country descriptor")
        if row.get("countryiso3code") != registry.dataset["country_code"]:
            raise WDIError(f"WDI row {position} is not China")
        year_text = row.get("date")
        if type(year_text) is not str or not re.fullmatch(r"\d{4}", year_text):
            raise WDIError(f"WDI row {position} has an invalid annual period")
        year = int(year_text)
        if not start_year <= year <= end_year:
            raise WDIError(f"WDI row {position} lies outside the requested year range")
        identity = (indicator_id, year)
        if identity in seen:
            raise WDIError(f"duplicate WDI row {indicator_id} {year}")
        seen.add(identity)
        represented.add(indicator_id)
        _bounded_source_text(
            row.get("unit"),
            path=f"WDI row {position} unit",
            maximum_bytes=256,
            allow_empty=True,
        )
        _bounded_source_text(
            row.get("scale", ""),
            path=f"WDI row {position} scale",
            maximum_bytes=256,
            allow_empty=True,
        )
        _bounded_source_text(
            row.get("obs_status"),
            path=f"WDI row {position} obs_status",
            maximum_bytes=256,
            allow_empty=True,
        )
        decimal = row.get("decimal")
        if isinstance(decimal, bool) or not isinstance(decimal, int) or not 0 <= decimal <= 15:
            raise WDIError(f"WDI row {position} decimal is invalid")
        footnote_text = _bounded_source_text(
            row.get("footnote"),
            path=f"WDI row {position} footnote",
            maximum_bytes=MAX_FOOTNOTE_BYTES,
            allow_empty=True,
        )
        footnote = footnote_text if footnote_text.strip() else None
        available = row.get("value") is not None
        availability.append(
            WDIAvailability(
                indicator_id=indicator_id,
                year=year,
                available=available,
                footnote=footnote,
            )
        )
        if not available:
            null_rows += 1
            continue
        binding = registry.bindings[indicator_id]
        populated.add(indicator_id)
        observations.append(
            EconomicObservation(
                series_id=binding.series_id,
                value=_number(row["value"], indicator_id=indicator_id, year=year),
                unit=binding.unit,
                frequency="A",
                period_start=date(year, 1, 1),
                period_end=date(year, 12, 31),
                released_at=released_at,
                collected_at=collected_at,
                source_id=registry.dataset["source_id"],
                evidence_url=evidence_url,
                revision=0,
                status="estimate",
                geography="CN",
                sector="all",
                quality=binding.quality,
                raw_sha256=_row_fingerprint(row),
                metadata={
                    "family": registry.dataset["independence_group"],
                    "source_series_id": indicator_id,
                    "source_document_version": last_updated.isoformat(),
                    "parser_version": PARSER_VERSION,
                    "release_time_semantics": registry.dataset[
                        "release_time_semantics"
                    ],
                    "aggregation_window": "calendar_year",
                },
            )
        )
    missing = sorted(set(registry.bindings) - represented)
    if missing:
        raise WDIError(
            "WDI response omits configured indicators: " + ", ".join(missing[:8])
        )
    indicator_provenance = tuple(
        WDIIndicatorProvenance(
            indicator_id=indicator_id,
            source_title=source_titles[indicator_id],
            reviewed_name=registry.bindings[indicator_id].name,
        )
        for indicator_id in sorted(source_titles)
    )
    ordered_availability = tuple(
        sorted(availability, key=lambda item: (item.indicator_id, item.year))
    )
    receipt_bytes = json.dumps(
        {
            "indicator_provenance": [row.to_dict() for row in indicator_provenance],
            "availability": [row.to_dict() for row in ordered_availability],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(receipt_bytes) > MAX_PROVENANCE_RECEIPT_BYTES:
        raise WDIError(
            "WDI provenance/availability receipt exceeds "
            f"{MAX_PROVENANCE_RECEIPT_BYTES} bytes"
        )
    return WDIResponse(
        observations=tuple(
            sorted(observations, key=lambda row: (row.series_id, row.period_start))
        ),
        raw_sha256=raw_sha256,
        evidence_url=evidence_url,
        dataset_last_updated=last_updated,
        source_rows=len(rows),
        null_rows=null_rows,
        requested_start_year=start_year,
        requested_end_year=end_year,
        represented_indicators=tuple(sorted(represented)),
        populated_indicators=tuple(sorted(populated)),
        indicator_provenance=indicator_provenance,
        availability=ordered_availability,
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
    """Fetch a bounded response while enforcing kill, rate and host controls."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise WDIError("WDI fetch URL must use the reviewed API host")
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
            if type(raw) is not bytes:
                raise WDIError("WDI fetcher did not return exact bytes")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise WDIError(f"WDI response exceeds {MAX_RESPONSE_BYTES} bytes")
            if not raw:
                raise WDIError("WDI response is empty")
            return raw
        except (FetchError, OSError, WDIError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(float(attempt + 1))
    raise WDIError(f"WDI fetch failed after {retries + 1} attempts: {last_error}")


def collect(
    registry: WDIRegistry,
    *,
    start_year: int,
    end_year: int,
    fetch: Callable[[str], bytes] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> WDIResponse:
    """Fetch one exact response, then sample its collection clock.

    ``clock`` is a callable rather than an already evaluated timestamp so no
    caller can accidentally claim to know bytes before the transport returned.
    """

    clock_fn = clock or (lambda: datetime.now(UTC))
    if not callable(clock_fn):
        raise WDIError("WDI collection clock must be callable")
    url = build_url(registry, start_year=start_year, end_year=end_year)
    raw = fetch(url) if fetch is not None else fetch_bytes(url)
    if type(raw) is not bytes:
        raise WDIError("WDI fetcher did not return exact bytes")
    if not raw:
        raise WDIError("WDI response is empty")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise WDIError(f"WDI response exceeds {MAX_RESPONSE_BYTES} bytes")
    collected_at = clock_fn()
    if not isinstance(collected_at, datetime):
        raise WDIError("WDI collection clock must return a datetime")
    return parse_response(
        raw,
        registry=registry,
        evidence_url=url,
        start_year=start_year,
        end_year=end_year,
        collected_at=collected_at,
    )


__all__ = [
    "PARSER_VERSION",
    "WDIError",
    "WDIAvailability",
    "WDIIndicatorProvenance",
    "WDIRegistry",
    "WDIResponse",
    "WDISeriesBinding",
    "build_url",
    "collect",
    "fetch_bytes",
    "load_registry",
    "parse_response",
]
