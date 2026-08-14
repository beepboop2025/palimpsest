"""Review-gated normalization of captured China primary documents.

This module is the boundary between four source-shaped HTML parsers and the
public bitemporal economic-observation contract.  Parsers return deliberately
small, source-labelled rows; this processor resolves those labels through a
reviewed registry and binds every observation to an authenticated
EvidenceDocument manifest.

Nothing in this module fetches the network or changes source activation state.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.econ_observation import EconomicObservation
from core.evidence_documents import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALIAS_PATH = ROOT / "config" / "china_econ_source_aliases.json"
DEFAULT_SERIES_PATH = ROOT / "config" / "china_econ_series.json"

ALIAS_SCHEMA_VERSION = "palimpsest-china-econ-source-aliases.v1"
SERIES_SCHEMA_VERSION = "palimpsest-china-econ-series.v1"
METHOD_VERSION = "primary-document-adapter.v1"
MAX_CONFIG_BYTES = 512 * 1024
MAX_PARSED_ROWS = 2_000

_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,159}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Configuration may rename neither a source nor its parser.  Widening this
# closed set requires a code review as well as a JSON edit.
_CLOSED_ADAPTERS = {
    "mot-transport": ("mot_transport", "collectors.mot_transport"),
    "spb-parcels": ("spb_parcels", "collectors.spb_parcels"),
    "nea-electricity": ("nea_electricity", "collectors.nea_electricity"),
    "nbs-70-city-housing": (
        "nbs_70_city_housing",
        "collectors.nbs_housing",
    ),
}

_ALIAS_FIELDS = frozenset(
    {
        "primary_source_id",
        "economic_source_id",
        "parser_module",
        "parser_version",
        "review_status",
    }
)
_SERIES_FIELDS = frozenset(
    {
        "primary_source_id",
        "economic_source_id",
        "parser_series_key",
        "series_id",
        "name",
        "unit",
        "source_units",
        "frequency",
        "aggregation_window",
        "geography_group",
        "sector_group",
        "source_table_id",
        "quality",
    }
)
_PARSED_FIELDS = frozenset(
    {
        "series_key",
        "value",
        "source_unit",
        "frequency",
        "period_start",
        "period_end",
        "aggregation_window",
        "geography_key",
        "sector_key",
        "source_table_id",
    }
)


class PrimaryEconomicAdapterError(ValueError):
    """A registry, captured document, or parsed row failed closed."""


class PrimaryEconomicRegistryError(PrimaryEconomicAdapterError):
    """The reviewed alias or series registry is malformed or broadened."""


@dataclass(frozen=True, slots=True)
class SourceAlias:
    primary_source_id: str
    economic_source_id: str
    parser_module: str
    parser_version: str


@dataclass(frozen=True, slots=True)
class SourceAliasRegistry:
    aliases: Mapping[str, SourceAlias]
    sha256: str


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    primary_source_id: str
    economic_source_id: str
    parser_series_key: str
    series_id: str
    name: str
    unit: str
    source_units: tuple[str, ...]
    frequency: str
    aggregation_window: str
    geography_group: str
    sector_group: str
    source_table_id: str
    quality: float


@dataclass(frozen=True, slots=True)
class SeriesRegistry:
    series: Mapping[tuple[str, str], SeriesSpec]
    geographies: Mapping[str, str]
    sectors: Mapping[str, str]
    geography_groups: Mapping[str, frozenset[str]]
    sector_groups: Mapping[str, frozenset[str]]
    sha256: str


def _duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise PrimaryEconomicRegistryError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _load_json(path: str | Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    location = Path(path)
    raw = location.read_bytes()
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise PrimaryEconomicRegistryError(
            f"{label} must be 1..{MAX_CONFIG_BYTES} bytes"
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite number {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PrimaryEconomicRegistryError(f"invalid {label}: {exc}") from exc
    if type(value) is not dict:
        raise PrimaryEconomicRegistryError(f"{label} must be a JSON object")
    return value, raw


def _exact(value: object, fields: frozenset[str], path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise PrimaryEconomicRegistryError(f"{path} must be an object")
    actual = set(value)
    if actual != fields:
        raise PrimaryEconomicRegistryError(
            f"{path} fields differ; missing={sorted(fields - actual)}, "
            f"extra={sorted(actual - fields)}"
        )
    return value


def _text(value: object, path: str, *, maximum: int = 240) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise PrimaryEconomicRegistryError(f"{path} must be bounded non-empty text")
    return value


def _identifier(value: object, path: str) -> str:
    text = _text(value, path, maximum=160)
    if not _ID_RE.fullmatch(text):
        raise PrimaryEconomicRegistryError(f"{path} is not a safe identifier")
    return text


def _string_map(value: object, path: str) -> dict[str, str]:
    if type(value) is not dict or not value or len(value) > 256:
        raise PrimaryEconomicRegistryError(f"{path} must be a bounded non-empty map")
    out: dict[str, str] = {}
    for key, item in value.items():
        source_key = _text(key, f"{path} key", maximum=80)
        canonical = _text(item, f"{path}.{source_key}", maximum=120)
        if source_key in out:
            raise PrimaryEconomicRegistryError(f"{path} contains a duplicate key")
        out[source_key] = canonical
    if len(set(out.values())) != len(out):
        raise PrimaryEconomicRegistryError(f"{path} canonical values must be unique")
    return out


def _groups(
    value: object,
    path: str,
    *,
    known_values: Mapping[str, str],
) -> dict[str, frozenset[str]]:
    if type(value) is not dict or not value or len(value) > 64:
        raise PrimaryEconomicRegistryError(f"{path} must be a bounded non-empty map")
    out: dict[str, frozenset[str]] = {}
    for key, members in value.items():
        group = _identifier(key, f"{path} key")
        if type(members) is not list or not members or len(members) > 256:
            raise PrimaryEconomicRegistryError(f"{path}.{group} must be a non-empty list")
        if any(type(member) is not str for member in members):
            raise PrimaryEconomicRegistryError(f"{path}.{group} members must be strings")
        if len(set(members)) != len(members):
            raise PrimaryEconomicRegistryError(f"{path}.{group} repeats a member")
        unknown = set(members) - set(known_values)
        if unknown:
            raise PrimaryEconomicRegistryError(
                f"{path}.{group} contains unknown values {sorted(unknown)}"
            )
        out[group] = frozenset(members)
    return out


def load_source_aliases(
    path: str | Path = DEFAULT_ALIAS_PATH,
) -> SourceAliasRegistry:
    document, raw = _load_json(path, label="China economic source aliases")
    top = _exact(
        document,
        frozenset({"schema_version", "aliases"}),
        "source alias registry",
    )
    if top["schema_version"] != ALIAS_SCHEMA_VERSION:
        raise PrimaryEconomicRegistryError("unsupported source alias registry version")
    rows = top["aliases"]
    if type(rows) is not list or len(rows) != len(_CLOSED_ADAPTERS):
        raise PrimaryEconomicRegistryError("source aliases must cover the closed tranche")
    aliases: dict[str, SourceAlias] = {}
    economic_ids: set[str] = set()
    for index, value in enumerate(rows):
        row = _exact(value, _ALIAS_FIELDS, f"aliases[{index}]")
        primary = _identifier(row["primary_source_id"], f"aliases[{index}].primary_source_id")
        economic = _identifier(
            row["economic_source_id"], f"aliases[{index}].economic_source_id"
        )
        module = _identifier(row["parser_module"], f"aliases[{index}].parser_module")
        parser_version = _identifier(
            row["parser_version"], f"aliases[{index}].parser_version"
        )
        if row["review_status"] != "reviewed_not_live":
            raise PrimaryEconomicRegistryError(
                f"{primary}: review_status must remain reviewed_not_live"
            )
        expected = _CLOSED_ADAPTERS.get(primary)
        if expected != (economic, module):
            raise PrimaryEconomicRegistryError(
                f"{primary}: alias or parser broadens the reviewed mapping"
            )
        if primary in aliases or economic in economic_ids:
            raise PrimaryEconomicRegistryError("source aliases must be one-to-one")
        aliases[primary] = SourceAlias(primary, economic, module, parser_version)
        economic_ids.add(economic)
    if set(aliases) != set(_CLOSED_ADAPTERS):
        raise PrimaryEconomicRegistryError("source aliases do not match the closed tranche")
    return SourceAliasRegistry(
        aliases=aliases,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_series_registry(
    path: str | Path = DEFAULT_SERIES_PATH,
    *,
    aliases: SourceAliasRegistry | None = None,
) -> SeriesRegistry:
    alias_registry = aliases or load_source_aliases()
    document, raw = _load_json(path, label="China economic series registry")
    top = _exact(
        document,
        frozenset({"schema_version", "dimensions", "series"}),
        "series registry",
    )
    if top["schema_version"] != SERIES_SCHEMA_VERSION:
        raise PrimaryEconomicRegistryError("unsupported series registry version")
    dimensions = _exact(
        top["dimensions"],
        frozenset(
            {"geographies", "sectors", "geography_groups", "sector_groups"}
        ),
        "series registry.dimensions",
    )
    geographies = _string_map(dimensions["geographies"], "dimensions.geographies")
    sectors = _string_map(dimensions["sectors"], "dimensions.sectors")
    geography_groups = _groups(
        dimensions["geography_groups"],
        "dimensions.geography_groups",
        known_values=geographies,
    )
    sector_groups = _groups(
        dimensions["sector_groups"],
        "dimensions.sector_groups",
        known_values=sectors,
    )

    rows = top["series"]
    if type(rows) is not list or not rows or len(rows) > 128:
        raise PrimaryEconomicRegistryError("series must be a bounded non-empty list")
    series: dict[tuple[str, str], SeriesSpec] = {}
    ids: set[str] = set()
    for index, value in enumerate(rows):
        row = _exact(value, _SERIES_FIELDS, f"series[{index}]")
        primary = _identifier(row["primary_source_id"], f"series[{index}].primary_source_id")
        economic = _identifier(row["economic_source_id"], f"series[{index}].economic_source_id")
        alias = alias_registry.aliases.get(primary)
        if alias is None or alias.economic_source_id != economic:
            raise PrimaryEconomicRegistryError(
                f"series[{index}] does not use a reviewed source alias"
            )
        parser_key = _identifier(
            row["parser_series_key"], f"series[{index}].parser_series_key"
        )
        series_id = _identifier(row["series_id"], f"series[{index}].series_id")
        name = _text(row["name"], f"series[{index}].name")
        unit = _text(row["unit"], f"series[{index}].unit", maximum=120)
        source_units = row["source_units"]
        if type(source_units) is not list or not source_units or len(source_units) > 8:
            raise PrimaryEconomicRegistryError(
                f"series[{index}].source_units must be unique bounded strings"
            )
        normalized_source_units = tuple(
            _text(item, f"series[{index}].source_units", maximum=120)
            for item in source_units
        )
        if len(set(normalized_source_units)) != len(normalized_source_units):
            raise PrimaryEconomicRegistryError(
                f"series[{index}].source_units must be unique bounded strings"
            )
        frequency = row["frequency"]
        if frequency not in {"M"}:
            raise PrimaryEconomicRegistryError(
                f"series[{index}].frequency is outside the reviewed tranche"
            )
        aggregation_window = _identifier(
            row["aggregation_window"], f"series[{index}].aggregation_window"
        )
        geography_group = _identifier(
            row["geography_group"], f"series[{index}].geography_group"
        )
        sector_group = _identifier(row["sector_group"], f"series[{index}].sector_group")
        if geography_group not in geography_groups or sector_group not in sector_groups:
            raise PrimaryEconomicRegistryError(
                f"series[{index}] references an unknown dimension group"
            )
        source_table_id = _identifier(
            row["source_table_id"], f"series[{index}].source_table_id"
        )
        quality = row["quality"]
        if isinstance(quality, bool) or not isinstance(quality, (int, float)):
            raise PrimaryEconomicRegistryError(f"series[{index}].quality must be numeric")
        quality = float(quality)
        if not math.isfinite(quality) or not 0.0 <= quality <= 1.0:
            raise PrimaryEconomicRegistryError(
                f"series[{index}].quality must lie in [0, 1]"
            )
        key = (primary, parser_key)
        if key in series or series_id in ids:
            raise PrimaryEconomicRegistryError("series keys and series_id values must be unique")
        series[key] = SeriesSpec(
            primary_source_id=primary,
            economic_source_id=economic,
            parser_series_key=parser_key,
            series_id=series_id,
            name=name,
            unit=unit,
            source_units=normalized_source_units,
            frequency=frequency,
            aggregation_window=aggregation_window,
            geography_group=geography_group,
            sector_group=sector_group,
            source_table_id=source_table_id,
            quality=quality,
        )
        ids.add(series_id)
    for primary in alias_registry.aliases:
        if not any(key[0] == primary for key in series):
            raise PrimaryEconomicRegistryError(f"{primary}: no reviewed series are registered")
    return SeriesRegistry(
        series=series,
        geographies=geographies,
        sectors=sectors,
        geography_groups=geography_groups,
        sector_groups=sector_groups,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _timestamp(value: object, path: str) -> datetime:
    if type(value) is not str or not value:
        raise PrimaryEconomicAdapterError(f"{path} is required; no time is inferred")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrimaryEconomicAdapterError(f"{path} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PrimaryEconomicAdapterError(f"{path} must be timezone-aware")
    return parsed


def _document_provenance(
    raw: bytes,
    *,
    document: Mapping[str, Any],
    vintage: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> tuple[str, str, datetime, datetime]:
    """Authenticate the public receipt, private manifest and exact bytes."""

    if type(raw) is not bytes or not raw:
        raise PrimaryEconomicAdapterError("captured document must be non-empty exact bytes")
    if len(raw) > 8 * 1024 * 1024:
        raise PrimaryEconomicAdapterError("captured document exceeds the parser byte bound")
    primary = document.get("source_id")
    if type(primary) is not str:
        raise PrimaryEconomicAdapterError("document.source_id is required")
    if document.get("capture_scope") != "release_document":
        raise PrimaryEconomicAdapterError(f"{primary}: only release_document capture is parseable")
    if document.get("media_type") != "text/html":
        raise PrimaryEconomicAdapterError(f"{primary}: only captured HTML is parseable")
    content_hash = hashlib.sha256(raw).hexdigest()
    if vintage.get("content_sha256") != content_hash:
        raise PrimaryEconomicAdapterError(f"{primary}: receipt/content SHA-256 mismatch")
    if vintage.get("byte_size") != len(raw):
        raise PrimaryEconomicAdapterError(f"{primary}: receipt/content byte-size mismatch")
    manifest_hash = vintage.get("manifest_sha256")
    if type(manifest_hash) is not str or not _SHA_RE.fullmatch(manifest_hash):
        raise PrimaryEconomicAdapterError(f"{primary}: manifest SHA-256 is invalid")
    if hashlib.sha256(canonical_json_bytes(manifest)).hexdigest() != manifest_hash:
        raise PrimaryEconomicAdapterError(f"{primary}: manifest hash does not match receipt")
    content = manifest.get("content")
    source = manifest.get("source")
    acceptance = manifest.get("acceptance")
    if type(content) is not dict or type(source) is not dict or type(acceptance) is not dict:
        raise PrimaryEconomicAdapterError(f"{primary}: incomplete EvidenceDocument manifest")
    if content != {"sha256": content_hash, "byte_size": len(raw)}:
        raise PrimaryEconomicAdapterError(f"{primary}: manifest/content binding mismatch")
    if source.get("id") != primary or source.get("canonical_url") != document.get("original_url"):
        raise PrimaryEconomicAdapterError(f"{primary}: manifest source binding mismatch")
    if manifest.get("media_type") != "text/html":
        raise PrimaryEconomicAdapterError(f"{primary}: manifest media type is not HTML")
    if manifest.get("publication_time") != vintage.get("publication_time"):
        raise PrimaryEconomicAdapterError(f"{primary}: publication clocks disagree")
    if manifest.get("collected_at") != vintage.get("first_retrieved_at"):
        raise PrimaryEconomicAdapterError(f"{primary}: collection clocks disagree")
    if acceptance.get("accepted_at") != vintage.get("accepted_at"):
        raise PrimaryEconomicAdapterError(f"{primary}: acceptance clocks disagree")
    released_at = _timestamp(manifest.get("publication_time"), "manifest.publication_time")
    collected_at = _timestamp(manifest.get("collected_at"), "manifest.collected_at")
    if collected_at < released_at:
        raise PrimaryEconomicAdapterError(f"{primary}: collection precedes publication")
    return content_hash, manifest_hash, released_at, collected_at


def _parsed_row(value: object, position: int) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _PARSED_FIELDS:
        raise PrimaryEconomicAdapterError(
            f"parsed row {position} does not match the closed parser row contract"
        )
    row = value
    for field in (
        "series_key",
        "source_unit",
        "frequency",
        "aggregation_window",
        "geography_key",
        "sector_key",
        "source_table_id",
    ):
        if type(row[field]) is not str or not row[field]:
            raise PrimaryEconomicAdapterError(f"parsed row {position}.{field} is required")
    if isinstance(row["value"], bool) or not isinstance(row["value"], (int, float)):
        raise PrimaryEconomicAdapterError(f"parsed row {position}.value must be numeric")
    if not math.isfinite(float(row["value"])):
        raise PrimaryEconomicAdapterError(f"parsed row {position}.value must be finite")
    if type(row["period_start"]) is not date or type(row["period_end"]) is not date:
        raise PrimaryEconomicAdapterError(f"parsed row {position} periods must be dates")
    if row["period_end"] < row["period_start"]:
        raise PrimaryEconomicAdapterError(f"parsed row {position} period is reversed")
    return row


def observations_from_captured_document(
    raw: bytes,
    *,
    document: Mapping[str, Any],
    vintage: Mapping[str, Any],
    manifest: Mapping[str, Any],
    aliases: SourceAliasRegistry,
    series_registry: SeriesRegistry,
) -> tuple[EconomicObservation, ...]:
    """Parse one authenticated document vintage into aggregate observations."""

    primary = document.get("source_id")
    alias = aliases.aliases.get(primary)
    if alias is None:
        raise PrimaryEconomicAdapterError(f"{primary!r} is outside the reviewed adapter tranche")
    content_hash, manifest_hash, released_at, collected_at = _document_provenance(
        raw,
        document=document,
        vintage=vintage,
        manifest=manifest,
    )
    module = importlib.import_module(alias.parser_module)
    if getattr(module, "PARSER_VERSION", None) != alias.parser_version:
        raise PrimaryEconomicAdapterError(
            f"{primary}: parser version does not match the reviewed alias registry"
        )
    parser = getattr(module, "parse", None)
    if not callable(parser):
        raise PrimaryEconomicAdapterError(f"{primary}: parser module has no parse()")
    parsed = parser(raw)
    if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes)):
        raise PrimaryEconomicAdapterError(f"{primary}: parser result must be a sequence")
    if not parsed or len(parsed) > MAX_PARSED_ROWS:
        raise PrimaryEconomicAdapterError(
            f"{primary}: parser must emit 1..{MAX_PARSED_ROWS} aggregate rows"
        )

    observations: list[EconomicObservation] = []
    semantic_keys: set[tuple[object, ...]] = set()
    for position, value in enumerate(parsed, 1):
        row = _parsed_row(value, position)
        spec = series_registry.series.get((primary, row["series_key"]))
        if spec is None:
            raise PrimaryEconomicAdapterError(
                f"{primary}: parsed series {row['series_key']!r} is not reviewed"
            )
        if spec.economic_source_id != alias.economic_source_id:
            raise PrimaryEconomicAdapterError(f"{primary}: series/source alias mismatch")
        if row["source_unit"] not in spec.source_units:
            raise PrimaryEconomicAdapterError(
                f"{primary}/{row['series_key']}: unexpected source unit {row['source_unit']!r}"
            )
        for field in ("frequency", "aggregation_window", "source_table_id"):
            if row[field] != getattr(spec, field):
                raise PrimaryEconomicAdapterError(
                    f"{primary}/{row['series_key']}: {field} differs from reviewed semantics"
                )
        if row["geography_key"] not in series_registry.geography_groups[spec.geography_group]:
            raise PrimaryEconomicAdapterError(
                f"{primary}/{row['series_key']}: geography is outside the reviewed group"
            )
        if row["sector_key"] not in series_registry.sector_groups[spec.sector_group]:
            raise PrimaryEconomicAdapterError(
                f"{primary}/{row['series_key']}: sector is outside the reviewed group"
            )
        geography = series_registry.geographies[row["geography_key"]]
        sector = series_registry.sectors[row["sector_key"]]
        semantic_key = (
            spec.series_id,
            row["period_start"],
            row["period_end"],
            geography,
            sector,
        )
        if semantic_key in semantic_keys:
            raise PrimaryEconomicAdapterError(f"{primary}: duplicate parsed observation slice")
        semantic_keys.add(semantic_key)
        aggregation_level = "city" if geography.startswith("CN:city:") else "national"
        observations.append(
            EconomicObservation(
                series_id=spec.series_id,
                value=float(row["value"]),
                unit=spec.unit,
                frequency=spec.frequency,
                period_start=row["period_start"],
                period_end=row["period_end"],
                released_at=released_at,
                collected_at=collected_at,
                source_id=alias.economic_source_id,
                evidence_url=str(document["original_url"]),
                status="observed",
                geography=geography,
                sector=sector,
                quality=spec.quality,
                raw_sha256=content_hash,
                metadata={
                    "family": primary,
                    "method_version": (
                        f"{METHOD_VERSION};aliases={aliases.sha256};"
                        f"series={series_registry.sha256}"
                    ),
                    "parser_version": alias.parser_version,
                    "schema_version": SERIES_SCHEMA_VERSION,
                    "release_time_semantics": (
                        "publisher publication_time authenticated by the immutable "
                        "EvidenceDocument manifest"
                    ),
                    "aggregation_window": spec.aggregation_window,
                    "aggregation_level": aggregation_level,
                    "source_series_id": spec.parser_series_key,
                    "source_table_id": spec.source_table_id,
                    "source_release_id": str(vintage["vintage_id"]),
                    "source_document_sha256": content_hash,
                    "source_manifest_sha256": manifest_hash,
                    "source_document_version": str(vintage["vintage_id"]),
                },
            )
        )
    return tuple(
        sorted(
            observations,
            key=lambda row: (
                row.period_end,
                row.series_id,
                row.geography,
                row.sector,
            ),
        )
    )


__all__ = [
    "ALIAS_SCHEMA_VERSION",
    "DEFAULT_ALIAS_PATH",
    "DEFAULT_SERIES_PATH",
    "METHOD_VERSION",
    "PrimaryEconomicAdapterError",
    "PrimaryEconomicRegistryError",
    "SERIES_SCHEMA_VERSION",
    "SeriesRegistry",
    "SourceAliasRegistry",
    "load_series_registry",
    "load_source_aliases",
    "observations_from_captured_document",
]
