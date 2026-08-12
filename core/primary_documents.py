"""Closed, revision-safe capture of high-value primary source documents.

The collector preserves exact bytes in :class:`EvidenceDocumentStore` and
publishes only a scrubbed receipt index.  A captured catalog or release is
primary evidence, but it is not automatically a parsed economic observation.
That distinction is represented by ``capture_scope`` and ``observation_state``
and is validated at every boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from core.evidence_documents import EvidenceDocumentStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "primary_document_sources.json"
DEFAULT_OUTPUT_PATH = ROOT / "readings" / "primary-documents-latest.json"

REGISTRY_SCHEMA_VERSION = "palimpsest-primary-document-sources.v1"
INDEX_SCHEMA_VERSION = "palimpsest-primary-documents.v1"
MAX_SOURCES = 64
MAX_VINTAGES_PER_SOURCE = 2048
MAX_TEXT = 8_192
_SAFE_INTEGER = 9_007_199_254_740_991

FetchBytes = Callable[..., bytes]


class PrimaryDocumentError(ValueError):
    """The source registry, capture, or public receipt failed its contract."""


class PrimaryDocumentRegistryError(PrimaryDocumentError):
    """The closed primary-source registry was malformed or broadened."""


class InvalidPrimaryDocument(PrimaryDocumentError):
    """Fetched bytes did not match the source's declared document type."""


_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_OBJECT_ID_RE = re.compile(r"^(?:document|documentv)-[0-9a-f]{24}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_LANGUAGE_RE = re.compile(r"^(?:und|[a-z]{2,8}(?:-[a-z0-9]{1,8})*)$")

_CAPTURE_SCOPES = frozenset(
    {"catalog_metadata", "release_document", "structured_observations"}
)
_OBSERVATION_STATES = frozenset({"not_parsed", "adapter_ready", "live"})
_CADENCES = frozenset({"event", "daily", "monthly", "quarterly", "annual"})
_MEDIA_TYPES = frozenset({"text/html", "application/json", "application/pdf"})

_SOURCE_FIELDS = frozenset(
    {
        "id",
        "name",
        "publisher",
        "url",
        "media_type",
        "language",
        "independence_group",
        "capture_scope",
        "observation_state",
        "cadence",
        "publication_time",
        "geographies",
        "sectors",
        "subjects",
        "units",
        "denominators",
        "methodology_url",
        "methodology_version",
        "methodology_notes",
        "rights",
        "interpretation_limit",
    }
)
_REGISTRY_FIELDS = frozenset({"schema_version", "max_document_bytes", "sources"})
_RIGHTS_FIELDS = frozenset({"training_use", "license_or_terms_ref"})

_VINTAGE_FIELDS = frozenset(
    {
        "vintage_id",
        "revision",
        "publication_time",
        "first_retrieved_at",
        "accepted_at",
        "collection_run_id",
        "content_sha256",
        "byte_size",
        "manifest_sha256",
        "supersedes_vintage_id",
    }
)
_DOCUMENT_FIELDS = frozenset(
    {
        "document_id",
        "source_id",
        "name",
        "publisher",
        "original_url",
        "role",
        "independence_group",
        "capture_scope",
        "observation_state",
        "media_type",
        "language",
        "cadence",
        "geographies",
        "sectors",
        "subjects",
        "units",
        "denominators",
        "methodology",
        "rights",
        "interpretation_limit",
        "last_checked_at",
        "retrieval_count",
        "current_vintage",
        "vintages",
    }
)
_METHODOLOGY_FIELDS = frozenset({"url", "version", "notes"})
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "last_successful_at",
        "source_registry",
        "source_registry_sha256",
        "scope",
        "method",
        "coverage",
        "n_documents",
        "n_vintages",
        "n_new_vintages",
        "documents",
    }
)
_COVERAGE_FIELDS = frozenset(
    {
        "status",
        "registered_sources",
        "attempted_sources",
        "successful_sources",
        "counts",
        "sources",
    }
)
_COVERAGE_SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "status",
        "attempted_at",
        "document_available",
        "retained_last_good",
        "document_id",
        "vintage_id",
        "content_sha256",
        "reason",
    }
)
_COVERAGE_STATUSES = frozenset(
    {"captured", "unchanged", "fetch_error", "invalid_document", "store_error"}
)

# Configuration cannot turn an arbitrary URL into a trusted primary source. A
# registry change must match this reviewed tuple exactly and therefore requires
# a code change as well as a data change.
_CLOSED_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "nbs-70-city-housing": (
        "https://www.stats.gov.cn/sj/zxfbhjd/202602/t20260213_1962617.html",
        "nbs-housing-prices",
        "text/html",
        "release_document",
    ),
    "nbs-national-macro": (
        "https://www.stats.gov.cn/english/PressRelease/",
        "nbs-official-statistics",
        "text/html",
        "catalog_metadata",
    ),
    "pboc-credit-tsf": (
        "https://www.pbc.gov.cn/diaochatongjisi/fileDir/resource/cms/2024/01/2024011510325158987.pdf",
        "pboc-credit-statistics",
        "application/pdf",
        "release_document",
    ),
    "gacc-trade": (
        "https://english.customs.gov.cn/Statistics/Statistics?ColumnId=1",
        "gacc-trade-statistics",
        "text/html",
        "catalog_metadata",
    ),
    "mot-transport": (
        "https://xxgk.mot.gov.cn/jigou/zhghs/202606/t20260629_4208435.html",
        "mot-transport-statistics",
        "text/html",
        "release_document",
    ),
    "spb-parcels": (
        "https://gs.spb.gov.cn/gssyzglj/c100062/c100149/202602/7eb1f16ae95344de900a86f36baa9e73.shtml",
        "spb-postal-statistics",
        "text/html",
        "release_document",
    ),
    "nea-electricity": (
        "https://www.nea.gov.cn/20260519/fdecbf091d654d53a9db438cdc356a5c/c.html",
        "nea-energy-statistics",
        "text/html",
        "release_document",
    ),
    "imf-portwatch": (
        "https://portwatch.imf.org/api/search/definition/",
        "imf-portwatch-ais",
        "text/html",
        "catalog_metadata",
    ),
    "sentinel5p-no2": (
        "https://documentation.dataspace.copernicus.eu/Data/SentinelMissions/Sentinel5P.html",
        "sentinel5p-atmosphere",
        "text/html",
        "catalog_metadata",
    ),
    "viirs-nightlights": (
        "https://ladsweb.modaps.eosdis.nasa.gov/missions-and-measurements/products/VNP46A1",
        "viirs-radiance",
        "text/html",
        "catalog_metadata",
    ),
    "hkex-filings": (
        "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en",
        "hkex-listed-company-disclosures",
        "text/html",
        "catalog_metadata",
    ),
    "sse-filings": (
        "https://www.sse.com.cn/disclosure/listedinfo/announcement/",
        "sse-listed-company-disclosures",
        "text/html",
        "catalog_metadata",
    ),
    "szse-filings": (
        "https://www.szse.cn/disclosure/listed/notice/index.html",
        "szse-listed-company-disclosures",
        "text/html",
        "catalog_metadata",
    ),
    "world-bank-enterprise-survey": (
        "https://microdata.worldbank.org/metadata/export/6676/json",
        "world-bank-enterprise-survey",
        "application/json",
        "catalog_metadata",
    ),
}


@dataclass(frozen=True, slots=True)
class PrimarySourceSpec:
    id: str
    name: str
    publisher: str
    url: str
    media_type: str
    language: str
    independence_group: str
    capture_scope: str
    observation_state: str
    cadence: str
    publication_time: str | None
    geographies: tuple[str, ...]
    sectors: tuple[str, ...]
    subjects: tuple[str, ...]
    units: tuple[str, ...]
    denominators: tuple[str, ...]
    methodology_url: str
    methodology_version: str
    methodology_notes: tuple[str, ...]
    rights: Mapping[str, str]
    interpretation_limit: str


@dataclass(frozen=True, slots=True)
class PrimarySourceRegistry:
    schema_version: str
    max_document_bytes: int
    sources: tuple[PrimarySourceSpec, ...]
    sha256: str


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise PrimaryDocumentRegistryError(f"duplicate JSON key: {key}")
        value[key] = child
    return value


def strict_json_loads(raw: bytes | str, *, label: str) -> Any:
    """Load strict UTF-8 JSON without duplicate keys or non-finite numbers."""

    try:
        text = raw.decode("utf-8", "strict") if type(raw) is bytes else raw
        if type(text) is not str:
            raise TypeError
        return json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PrimaryDocumentRegistryError(
                    f"{label} contains a non-finite number: {token}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise PrimaryDocumentRegistryError(f"{label} is not strict UTF-8 JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    try:
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
    except (TypeError, ValueError) as exc:
        raise PrimaryDocumentError("document is not finite canonical JSON") from exc


def _exact_fields(value: Any, expected: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise PrimaryDocumentError(f"{path} must be an object")
    actual = set(value)
    if actual != expected:
        raise PrimaryDocumentError(
            f"{path} fields do not match contract "
            f"(missing={sorted(expected - actual)}, unknown={sorted(actual - expected)})"
        )
    return value


def _text(value: Any, path: str, *, maximum: int = MAX_TEXT) -> str:
    if type(value) is not str:
        raise PrimaryDocumentError(f"{path} must be text")
    value = unicodedata.normalize("NFC", value)
    if not value.strip() or len(value) > maximum:
        raise PrimaryDocumentError(f"{path} has invalid length")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise PrimaryDocumentError(f"{path} contains unsafe Unicode")
    return value


def _identifier(value: Any, path: str) -> str:
    value = _text(value, path, maximum=80)
    if not _ID_RE.fullmatch(value):
        raise PrimaryDocumentError(f"{path} is not a safe identifier")
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TIMESTAMP_RE.fullmatch(value):
        raise PrimaryDocumentError(f"{path} is not a canonical UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise PrimaryDocumentError(f"{path} is not a real timestamp") from exc
    return value


def _timestamp_value(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def format_timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PrimaryDocumentError("capture clock must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _https_url(value: Any, path: str) -> str:
    value = _text(value, path, maximum=2048)
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise PrimaryDocumentError(f"{path} is not a valid URL") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise PrimaryDocumentError(f"{path} must be an uncredentialed HTTPS URL")
    return value


def _string_set(value: Any, path: str, *, maximum: int = 64) -> tuple[str, ...]:
    if type(value) is not list or not value or len(value) > maximum:
        raise PrimaryDocumentError(f"{path} must be a non-empty bounded array")
    result = tuple(_text(item, f"{path}[]", maximum=240) for item in value)
    if len(result) != len(set(result)):
        raise PrimaryDocumentError(f"{path} contains duplicates")
    return result


def load_primary_source_registry(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> PrimarySourceRegistry:
    """Load the exact reviewed source set and reject URL/role broadening."""

    raw = Path(path).read_bytes()
    data = strict_json_loads(raw, label="primary-document source registry")
    data = _exact_fields(data, _REGISTRY_FIELDS, "registry")
    if data["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise PrimaryDocumentRegistryError("unsupported source registry version")
    maximum = data["max_document_bytes"]
    if type(maximum) is not int or not 1 <= maximum <= 8 * 1024 * 1024:
        raise PrimaryDocumentRegistryError("max_document_bytes exceeds the v1 bound")
    if type(data["sources"]) is not list or not 1 <= len(data["sources"]) <= MAX_SOURCES:
        raise PrimaryDocumentRegistryError("sources must be a non-empty bounded array")
    if len(data["sources"]) != len(_CLOSED_SOURCES):
        raise PrimaryDocumentRegistryError("registry does not contain the exact v1 source set")

    sources: list[PrimarySourceSpec] = []
    seen: set[str] = set()
    for index, raw_source in enumerate(data["sources"]):
        path_name = f"sources[{index}]"
        row = _exact_fields(raw_source, _SOURCE_FIELDS, path_name)
        source_id = _identifier(row["id"], f"{path_name}.id")
        if source_id in seen or source_id not in _CLOSED_SOURCES:
            raise PrimaryDocumentRegistryError(
                f"{path_name}.id is duplicate or outside the closed registry"
            )
        seen.add(source_id)
        expected_url, expected_group, expected_media, expected_scope = _CLOSED_SOURCES[
            source_id
        ]
        url = _https_url(row["url"], f"{path_name}.url")
        media_type = _text(row["media_type"], f"{path_name}.media_type", maximum=80)
        group = _identifier(
            row["independence_group"], f"{path_name}.independence_group"
        )
        scope = _text(row["capture_scope"], f"{path_name}.capture_scope", maximum=40)
        if (url, group, media_type, scope) != (
            expected_url,
            expected_group,
            expected_media,
            expected_scope,
        ):
            raise PrimaryDocumentRegistryError(
                f"{path_name} broadens the reviewed URL, group, media type, or scope"
            )
        if media_type not in _MEDIA_TYPES or scope not in _CAPTURE_SCOPES:
            raise PrimaryDocumentRegistryError(f"{path_name} has an unsupported media/scope")
        observation_state = _text(
            row["observation_state"], f"{path_name}.observation_state", maximum=32
        )
        if observation_state not in _OBSERVATION_STATES:
            raise PrimaryDocumentRegistryError(
                f"{path_name}.observation_state is unsupported"
            )
        if observation_state == "live" and scope != "structured_observations":
            raise PrimaryDocumentRegistryError(
                f"{path_name} cannot call observations live without structured scope"
            )
        cadence = _text(row["cadence"], f"{path_name}.cadence", maximum=16)
        if cadence not in _CADENCES:
            raise PrimaryDocumentRegistryError(f"{path_name}.cadence is unsupported")
        publication_time = row["publication_time"]
        if publication_time is not None:
            publication_time = _timestamp(
                publication_time, f"{path_name}.publication_time"
            )
        language = _text(row["language"], f"{path_name}.language", maximum=32)
        if not _LANGUAGE_RE.fullmatch(language):
            raise PrimaryDocumentRegistryError(f"{path_name}.language is invalid")
        rights = _exact_fields(row["rights"], _RIGHTS_FIELDS, f"{path_name}.rights")
        if rights["training_use"] != "metadata_only":
            raise PrimaryDocumentRegistryError(
                f"{path_name}.rights must remain metadata_only until separate review"
            )
        normalized_rights = {
            "training_use": "metadata_only",
            "license_or_terms_ref": _https_url(
                rights["license_or_terms_ref"],
                f"{path_name}.rights.license_or_terms_ref",
            ),
        }
        sources.append(
            PrimarySourceSpec(
                id=source_id,
                name=_text(row["name"], f"{path_name}.name", maximum=200),
                publisher=_text(
                    row["publisher"], f"{path_name}.publisher", maximum=200
                ),
                url=url,
                media_type=media_type,
                language=language,
                independence_group=group,
                capture_scope=scope,
                observation_state=observation_state,
                cadence=cadence,
                publication_time=publication_time,
                geographies=_string_set(row["geographies"], f"{path_name}.geographies"),
                sectors=_string_set(row["sectors"], f"{path_name}.sectors"),
                subjects=_string_set(row["subjects"], f"{path_name}.subjects"),
                units=_string_set(row["units"], f"{path_name}.units"),
                denominators=_string_set(
                    row["denominators"], f"{path_name}.denominators"
                ),
                methodology_url=_https_url(
                    row["methodology_url"], f"{path_name}.methodology_url"
                ),
                methodology_version=_text(
                    row["methodology_version"],
                    f"{path_name}.methodology_version",
                    maximum=160,
                ),
                methodology_notes=_string_set(
                    row["methodology_notes"], f"{path_name}.methodology_notes", maximum=16
                ),
                rights=normalized_rights,
                interpretation_limit=_text(
                    row["interpretation_limit"],
                    f"{path_name}.interpretation_limit",
                    maximum=1000,
                ),
            )
        )
    if seen != set(_CLOSED_SOURCES):
        raise PrimaryDocumentRegistryError("registry does not match the closed source set")
    canonical = canonical_json_bytes(data)
    return PrimarySourceRegistry(
        schema_version=REGISTRY_SCHEMA_VERSION,
        max_document_bytes=maximum,
        sources=tuple(sources),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:24]}"


def _validate_fetched_bytes(raw: bytes, spec: PrimarySourceSpec) -> None:
    if type(raw) is not bytes or not raw:
        raise InvalidPrimaryDocument("source returned no exact bytes")
    if spec.media_type == "application/pdf":
        if not raw.startswith(b"%PDF-"):
            raise InvalidPrimaryDocument("source did not return a PDF document")
        return
    if spec.media_type == "application/json":
        try:
            strict_json_loads(raw, label=f"{spec.id} response")
        except PrimaryDocumentRegistryError as exc:
            raise InvalidPrimaryDocument(str(exc)) from exc
        return
    try:
        head = raw[:131_072].decode("utf-8-sig", "strict").casefold()
    except UnicodeDecodeError as exc:
        raise InvalidPrimaryDocument("HTML source is not strict UTF-8") from exc
    if "<html" not in head and "<!doctype html" not in head:
        raise InvalidPrimaryDocument("source did not return an HTML document")
    interstitial_markers = (
        "captcha",
        "access denied",
        "verify you are human",
        "安全验证",
        "访问验证",
    )
    if any(marker in head for marker in interstitial_markers):
        raise InvalidPrimaryDocument("source returned an access-control interstitial")


def _collection_metadata(
    spec: PrimarySourceSpec,
    *,
    collected_at: str,
    collection_run_id: str,
) -> dict[str, Any]:
    return {
        "source": {"id": spec.id, "canonical_url": spec.url},
        "media_type": spec.media_type,
        "language": spec.language,
        "event_time": spec.publication_time,
        "publication_time": spec.publication_time,
        "knowledge_time": spec.publication_time or collected_at,
        "collected_at": collected_at,
        "collection": {
            "run_id": collection_run_id,
            "parent_feed_sha256": None,
        },
        "retention_class": "primary-source-permanent",
        "rights": dict(spec.rights),
    }


def _document_id(spec: PrimarySourceSpec) -> str:
    return _stable_id("document", {"source_id": spec.id, "url": spec.url})


def _new_vintage(
    spec: PrimarySourceSpec,
    *,
    raw: bytes,
    stored: Any,
    revision: int,
    collected_at: str,
    run_id: str,
    supersedes_vintage_id: str | None,
) -> dict[str, Any]:
    content_sha256 = hashlib.sha256(raw).hexdigest()
    document_id = _document_id(spec)
    identity = {
        "document_id": document_id,
        "revision": revision,
        "content_sha256": content_sha256,
        "publication_time": spec.publication_time,
        "first_retrieved_at": collected_at,
    }
    return {
        "vintage_id": _stable_id("documentv", identity),
        "revision": revision,
        "publication_time": spec.publication_time,
        "first_retrieved_at": collected_at,
        "accepted_at": stored.accepted_at,
        "collection_run_id": run_id,
        "content_sha256": content_sha256,
        "byte_size": len(raw),
        "manifest_sha256": stored.manifest_sha256,
        "supersedes_vintage_id": supersedes_vintage_id,
    }


def _document_from_spec(
    spec: PrimarySourceSpec,
    *,
    vintage: Mapping[str, Any],
    vintages: Sequence[Mapping[str, Any]],
    checked_at: str,
    retrieval_count: int,
) -> dict[str, Any]:
    return {
        "document_id": _document_id(spec),
        "source_id": spec.id,
        "name": spec.name,
        "publisher": spec.publisher,
        "original_url": spec.url,
        "role": "primary",
        "independence_group": spec.independence_group,
        "capture_scope": spec.capture_scope,
        "observation_state": spec.observation_state,
        "media_type": spec.media_type,
        "language": spec.language,
        "cadence": spec.cadence,
        "geographies": list(spec.geographies),
        "sectors": list(spec.sectors),
        "subjects": list(spec.subjects),
        "units": list(spec.units),
        "denominators": list(spec.denominators),
        "methodology": {
            "url": spec.methodology_url,
            "version": spec.methodology_version,
            "notes": list(spec.methodology_notes),
        },
        "rights": dict(spec.rights),
        "interpretation_limit": spec.interpretation_limit,
        "last_checked_at": checked_at,
        "retrieval_count": retrieval_count,
        "current_vintage": dict(vintage),
        "vintages": [dict(row) for row in vintages],
    }


def _coverage_receipt(
    spec: PrimarySourceSpec,
    *,
    status: str,
    attempted_at: str,
    retained: Mapping[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    current = retained["current_vintage"] if retained else None
    return {
        "source_id": spec.id,
        "status": status,
        "attempted_at": attempted_at,
        "document_available": retained is not None,
        "retained_last_good": (
            retained is not None
            and status in {"fetch_error", "invalid_document", "store_error"}
        ),
        "document_id": retained["document_id"] if retained else None,
        "vintage_id": current["vintage_id"] if current else None,
        "content_sha256": current["content_sha256"] if current else None,
        "reason": _text(reason, "coverage.reason", maximum=500),
    }


def _validate_previous_for_registry(
    previous: Mapping[str, Any], registry: PrimarySourceRegistry
) -> None:
    """Accept a registry migration only when retained documents are unchanged.

    A source endpoint can be corrected without invalidating unrelated evidence
    when that source has never produced a document.  Once bytes have been
    accepted, however, every registry-derived field (including URL and stable
    document identity) is immutable in v1.
    """

    validate_primary_document_index(previous)
    if previous["source_registry_sha256"] == registry.sha256:
        validate_primary_document_index(previous, registry=registry)
        return

    sources_by_id = {source.id: source for source in registry.sources}
    receipt_ids = {row["source_id"] for row in previous["coverage"]["sources"]}
    if (
        previous["coverage"]["registered_sources"] != len(registry.sources)
        or previous["coverage"]["attempted_sources"] != len(registry.sources)
        or receipt_ids != set(sources_by_id)
    ):
        raise PrimaryDocumentError(
            "registry migration changes the closed source set"
        )

    for index, row in enumerate(previous["documents"]):
        spec = sources_by_id.get(row["source_id"])
        if spec is None:
            raise PrimaryDocumentError(
                f"documents[{index}] is outside the migrated registry"
            )
        expected = _document_from_spec(
            spec,
            vintage=row["current_vintage"],
            vintages=row["vintages"],
            checked_at=row["last_checked_at"],
            retrieval_count=row["retrieval_count"],
        )
        if row != expected:
            raise PrimaryDocumentError(
                "registry migration changes retained source metadata for "
                f"{row['source_id']}"
            )


def collect_primary_documents(
    registry: PrimarySourceRegistry,
    fetcher: FetchBytes,
    store: EvidenceDocumentStore,
    *,
    now: datetime,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture every reviewed source and build a deterministic public index.

    Source failures are represented in coverage and retain the previous valid
    document. They never create an empty document or erase a prior vintage.
    """

    generated_at = format_timestamp(now)
    if previous is not None:
        _validate_previous_for_registry(previous, registry)
    previous_by_source = {
        row["source_id"]: row for row in (previous["documents"] if previous else [])
    }
    documents: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    new_vintages = 0

    for spec in registry.sources:
        prior = previous_by_source.get(spec.id)
        try:
            raw = fetcher(
                spec.url,
                max_bytes=registry.max_document_bytes,
                timeout=30.0,
                max_redirects=0,
                headers={
                    "Accept": spec.media_type,
                    "User-Agent": (
                        "Palimpsest/0.5 (+https://palimpsest.info; "
                        "use=primary-document-archive)"
                    ),
                },
            )
            if type(raw) is not bytes or len(raw) > registry.max_document_bytes:
                raise InvalidPrimaryDocument("source response exceeded the byte contract")
            _validate_fetched_bytes(raw, spec)
        except InvalidPrimaryDocument as exc:
            if prior is not None:
                documents.append(dict(prior))
            receipts.append(
                _coverage_receipt(
                    spec,
                    status="invalid_document",
                    attempted_at=generated_at,
                    retained=prior,
                    reason=f"Document validation failed ({type(exc).__name__}).",
                )
            )
            continue
        except Exception as exc:  # transport implementations deliberately vary
            if prior is not None:
                documents.append(dict(prior))
            receipts.append(
                _coverage_receipt(
                    spec,
                    status="fetch_error",
                    attempted_at=generated_at,
                    retained=prior,
                    reason=f"Fetch failed ({type(exc).__name__}).",
                )
            )
            continue

        content_sha256 = hashlib.sha256(raw).hexdigest()
        unchanged = (
            prior is not None
            and prior["current_vintage"]["content_sha256"] == content_sha256
        )
        if unchanged:
            current = prior["current_vintage"]
            collected_at = current["first_retrieved_at"]
            run_id = current["collection_run_id"]
            revision = current["revision"]
            supersedes = current["supersedes_vintage_id"]
        else:
            collected_at = generated_at
            run_id = (
                "primary-documents-"
                + generated_at.translate({ord('-'): None, ord(':'): None}).lower()
            )
            revision = prior["current_vintage"]["revision"] + 1 if prior else 0
            supersedes = prior["current_vintage"]["vintage_id"] if prior else None
        try:
            stored = store.ingest(
                raw,
                _collection_metadata(
                    spec,
                    collected_at=collected_at,
                    collection_run_id=run_id,
                ),
            )
        except Exception as exc:
            if prior is not None:
                documents.append(dict(prior))
            receipts.append(
                _coverage_receipt(
                    spec,
                    status="store_error",
                    attempted_at=generated_at,
                    retained=prior,
                    reason=f"Private evidence commit failed ({type(exc).__name__}).",
                )
            )
            continue

        vintage = _new_vintage(
            spec,
            raw=raw,
            stored=stored,
            revision=revision,
            collected_at=collected_at,
            run_id=run_id,
            supersedes_vintage_id=supersedes,
        )
        if unchanged:
            if vintage != prior["current_vintage"]:
                documents.append(dict(prior))
                receipts.append(
                    _coverage_receipt(
                        spec,
                        status="store_error",
                        attempted_at=generated_at,
                        retained=prior,
                        reason="Private store receipt does not match the retained vintage.",
                    )
                )
                continue
            vintages = prior["vintages"]
            retrieval_count = prior["retrieval_count"] + 1
            status = "unchanged"
        else:
            vintages = [*(prior["vintages"] if prior else []), vintage]
            if len(vintages) > MAX_VINTAGES_PER_SOURCE:
                raise PrimaryDocumentError(
                    f"{spec.id} exceeds the v1 vintage bound; refusing truncation"
                )
            retrieval_count = (prior["retrieval_count"] if prior else 0) + 1
            status = "captured"
            new_vintages += 1
        document = _document_from_spec(
            spec,
            vintage=vintage,
            vintages=vintages,
            checked_at=generated_at,
            retrieval_count=retrieval_count,
        )
        documents.append(document)
        receipts.append(
            _coverage_receipt(
                spec,
                status=status,
                attempted_at=generated_at,
                retained=document,
                reason=(
                    "Exact bytes committed as a new immutable vintage."
                    if status == "captured"
                    else "Exact bytes match the retained immutable vintage."
                ),
            )
        )

    counts = {status: 0 for status in sorted(_COVERAGE_STATUSES)}
    for receipt in receipts:
        counts[receipt["status"]] += 1
    successful = counts["captured"] + counts["unchanged"]
    if successful == 0 and not documents:
        raise PrimaryDocumentError(
            "zero primary sources produced a valid document and no last-good index exists"
        )
    healthy = successful == len(registry.sources)
    documents = sorted(documents, key=lambda row: row["source_id"])
    document = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": generated_at,
        "last_successful_at": (
            generated_at
            if successful
            else previous["last_successful_at"]
        ),
        "source_registry": (
            "https://palimpsest.info/config/primary_document_sources.json"
        ),
        "source_registry_sha256": registry.sha256,
        "scope": (
            "Exact public primary-source release or catalog bytes from the closed "
            "registry; catalog capture is not a structured economic observation."
        ),
        "method": (
            "Bounded no-redirect fetch, media-shape validation, immutable private "
            "EvidenceDocument commit, content-addressed revision lineage, and a "
            "metadata-only public receipt projection."
        ),
        "coverage": {
            "status": "healthy" if healthy else "degraded",
            "registered_sources": len(registry.sources),
            "attempted_sources": len(receipts),
            "successful_sources": successful,
            "counts": counts,
            "sources": sorted(receipts, key=lambda row: row["source_id"]),
        },
        "n_documents": len(documents),
        "n_vintages": sum(len(row["vintages"]) for row in documents),
        "n_new_vintages": new_vintages,
        "documents": documents,
    }
    validate_primary_document_index(document, registry=registry)
    return document


def _validate_vintage(value: Any, path: str) -> Mapping[str, Any]:
    row = _exact_fields(value, _VINTAGE_FIELDS, path)
    if type(row["vintage_id"]) is not str or not _OBJECT_ID_RE.fullmatch(
        row["vintage_id"]
    ):
        raise PrimaryDocumentError(f"{path}.vintage_id is invalid")
    if type(row["revision"]) is not int or not 0 <= row["revision"] <= _SAFE_INTEGER:
        raise PrimaryDocumentError(f"{path}.revision is invalid")
    if row["publication_time"] is not None:
        _timestamp(row["publication_time"], f"{path}.publication_time")
    retrieved = _timestamp(row["first_retrieved_at"], f"{path}.first_retrieved_at")
    if row["publication_time"] is not None and _timestamp_value(
        row["publication_time"]
    ) > _timestamp_value(retrieved):
        raise PrimaryDocumentError(f"{path}.publication_time follows retrieval")
    accepted = _timestamp(row["accepted_at"], f"{path}.accepted_at")
    if _timestamp_value(accepted) < _timestamp_value(retrieved):
        raise PrimaryDocumentError(f"{path}.accepted_at precedes retrieval")
    _identifier(row["collection_run_id"], f"{path}.collection_run_id")
    for field in ("content_sha256", "manifest_sha256"):
        if type(row[field]) is not str or not _SHA_RE.fullmatch(row[field]):
            raise PrimaryDocumentError(f"{path}.{field} is invalid")
    if type(row["byte_size"]) is not int or not 1 <= row["byte_size"] <= 8 * 1024 * 1024:
        raise PrimaryDocumentError(f"{path}.byte_size is invalid")
    if row["supersedes_vintage_id"] is not None and (
        type(row["supersedes_vintage_id"]) is not str
        or not _OBJECT_ID_RE.fullmatch(row["supersedes_vintage_id"])
    ):
        raise PrimaryDocumentError(f"{path}.supersedes_vintage_id is invalid")
    return row


def validate_primary_document_index(
    document: Mapping[str, Any],
    *,
    registry: PrimarySourceRegistry | None = None,
) -> None:
    """Validate the complete public receipt projection without private bytes."""

    top = _exact_fields(document, _TOP_FIELDS, "index")
    if top["schema_version"] != INDEX_SCHEMA_VERSION:
        raise PrimaryDocumentError("unsupported primary-document index version")
    generated_at = _timestamp(top["generated_at"], "generated_at")
    last_successful_at = _timestamp(
        top["last_successful_at"], "last_successful_at"
    )
    if _timestamp_value(last_successful_at) > _timestamp_value(generated_at):
        raise PrimaryDocumentError("last_successful_at is later than generated_at")
    if top["source_registry"] != (
        "https://palimpsest.info/config/primary_document_sources.json"
    ):
        raise PrimaryDocumentError("source_registry URL is not canonical")
    if type(top["source_registry_sha256"]) is not str or not _SHA_RE.fullmatch(
        top["source_registry_sha256"]
    ):
        raise PrimaryDocumentError("source_registry_sha256 is invalid")
    _text(top["scope"], "scope")
    _text(top["method"], "method")
    if registry is not None and top["source_registry_sha256"] != registry.sha256:
        raise PrimaryDocumentError("source registry digest does not match the loaded registry")
    sources_by_id = {source.id: source for source in registry.sources} if registry else {}

    rows = top["documents"]
    if type(rows) is not list or len(rows) > MAX_SOURCES:
        raise PrimaryDocumentError("documents must be a bounded array")
    if rows != sorted(rows, key=lambda row: row.get("source_id", "")):
        raise PrimaryDocumentError("documents are not in deterministic source order")
    seen_sources: set[str] = set()
    seen_documents: set[str] = set()
    total_vintages = 0
    for index, value in enumerate(rows):
        path = f"documents[{index}]"
        row = _exact_fields(value, _DOCUMENT_FIELDS, path)
        source_id = _identifier(row["source_id"], f"{path}.source_id")
        if source_id in seen_sources:
            raise PrimaryDocumentError("duplicate source document")
        seen_sources.add(source_id)
        document_id = row["document_id"]
        if type(document_id) is not str or not _OBJECT_ID_RE.fullmatch(document_id):
            raise PrimaryDocumentError(f"{path}.document_id is invalid")
        if document_id in seen_documents:
            raise PrimaryDocumentError("duplicate document id")
        seen_documents.add(document_id)
        url = _https_url(row["original_url"], f"{path}.original_url")
        if document_id != _stable_id("document", {"source_id": source_id, "url": url}):
            raise PrimaryDocumentError(f"{path}.document_id does not match source/url")
        if row["role"] != "primary":
            raise PrimaryDocumentError(f"{path}.role must remain primary")
        _text(row["name"], f"{path}.name", maximum=200)
        _text(row["publisher"], f"{path}.publisher", maximum=200)
        _identifier(row["independence_group"], f"{path}.independence_group")
        if row["capture_scope"] not in _CAPTURE_SCOPES:
            raise PrimaryDocumentError(f"{path}.capture_scope is invalid")
        if row["observation_state"] not in _OBSERVATION_STATES:
            raise PrimaryDocumentError(f"{path}.observation_state is invalid")
        if row["observation_state"] == "live" and row["capture_scope"] != "structured_observations":
            raise PrimaryDocumentError(f"{path} makes an unsupported live-observation claim")
        if row["media_type"] not in _MEDIA_TYPES or row["cadence"] not in _CADENCES:
            raise PrimaryDocumentError(f"{path} media type/cadence is invalid")
        if type(row["language"]) is not str or not _LANGUAGE_RE.fullmatch(row["language"]):
            raise PrimaryDocumentError(f"{path}.language is invalid")
        for field in ("geographies", "sectors", "subjects", "units", "denominators"):
            _string_set(row[field], f"{path}.{field}")
        methodology = _exact_fields(
            row["methodology"], _METHODOLOGY_FIELDS, f"{path}.methodology"
        )
        _https_url(methodology["url"], f"{path}.methodology.url")
        _text(methodology["version"], f"{path}.methodology.version", maximum=160)
        _string_set(methodology["notes"], f"{path}.methodology.notes", maximum=16)
        rights = _exact_fields(row["rights"], _RIGHTS_FIELDS, f"{path}.rights")
        if rights["training_use"] != "metadata_only":
            raise PrimaryDocumentError(f"{path}.rights broadens document use")
        _https_url(rights["license_or_terms_ref"], f"{path}.rights.license_or_terms_ref")
        _text(row["interpretation_limit"], f"{path}.interpretation_limit", maximum=1000)
        checked_at = _timestamp(row["last_checked_at"], f"{path}.last_checked_at")
        if _timestamp_value(checked_at) > _timestamp_value(generated_at):
            raise PrimaryDocumentError(f"{path}.last_checked_at is future-dated")
        if type(row["retrieval_count"]) is not int or not 1 <= row["retrieval_count"] <= _SAFE_INTEGER:
            raise PrimaryDocumentError(f"{path}.retrieval_count is invalid")
        vintages = row["vintages"]
        if type(vintages) is not list or not 1 <= len(vintages) <= MAX_VINTAGES_PER_SOURCE:
            raise PrimaryDocumentError(f"{path}.vintages is outside the v1 bound")
        vintage_ids: set[str] = set()
        previous_vintage_id = None
        for revision, raw_vintage in enumerate(vintages):
            vintage = _validate_vintage(raw_vintage, f"{path}.vintages[{revision}]")
            if vintage["revision"] != revision:
                raise PrimaryDocumentError(f"{path}.vintages revisions are not contiguous")
            expected_vintage_id = _stable_id(
                "documentv",
                {
                    "document_id": document_id,
                    "revision": revision,
                    "content_sha256": vintage["content_sha256"],
                    "publication_time": vintage["publication_time"],
                    "first_retrieved_at": vintage["first_retrieved_at"],
                },
            )
            if vintage["vintage_id"] != expected_vintage_id:
                raise PrimaryDocumentError(f"{path}.vintage_id is not content-addressed")
            if vintage["vintage_id"] in vintage_ids:
                raise PrimaryDocumentError(f"{path}.vintages contains duplicate identity")
            vintage_ids.add(vintage["vintage_id"])
            if vintage["supersedes_vintage_id"] != previous_vintage_id:
                raise PrimaryDocumentError(f"{path}.vintage lineage is broken")
            if revision and _timestamp_value(vintage["first_retrieved_at"]) < _timestamp_value(
                vintages[revision - 1]["first_retrieved_at"]
            ):
                raise PrimaryDocumentError(f"{path}.vintage retrieval clock moved backwards")
            previous_vintage_id = vintage["vintage_id"]
        if row["current_vintage"] != vintages[-1]:
            raise PrimaryDocumentError(f"{path}.current_vintage is not the latest revision")
        total_vintages += len(vintages)
        if registry is not None:
            spec = sources_by_id.get(source_id)
            if spec is None:
                raise PrimaryDocumentError(f"{path} source is outside the loaded registry")
            expected_static = _document_from_spec(
                spec,
                vintage=row["current_vintage"],
                vintages=row["vintages"],
                checked_at=row["last_checked_at"],
                retrieval_count=row["retrieval_count"],
            )
            if row != expected_static:
                raise PrimaryDocumentError(f"{path} metadata does not match the loaded registry")

    for field, expected in (("n_documents", len(rows)), ("n_vintages", total_vintages)):
        if top[field] != expected:
            raise PrimaryDocumentError(f"{field} does not match documents")
    if type(top["n_new_vintages"]) is not int or not 0 <= top["n_new_vintages"] <= len(rows):
        raise PrimaryDocumentError("n_new_vintages is invalid")

    coverage = _exact_fields(top["coverage"], _COVERAGE_FIELDS, "coverage")
    if coverage["status"] not in {"healthy", "degraded"}:
        raise PrimaryDocumentError("coverage.status is invalid")
    for field in ("registered_sources", "attempted_sources", "successful_sources"):
        if type(coverage[field]) is not int or not 0 <= coverage[field] <= MAX_SOURCES:
            raise PrimaryDocumentError(f"coverage.{field} is invalid")
    counts = coverage["counts"]
    if type(counts) is not dict or set(counts) != _COVERAGE_STATUSES:
        raise PrimaryDocumentError("coverage.counts fields are invalid")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise PrimaryDocumentError("coverage.counts values are invalid")
    source_receipts = coverage["sources"]
    if type(source_receipts) is not list or source_receipts != sorted(
        source_receipts, key=lambda row: row.get("source_id", "")
    ):
        raise PrimaryDocumentError("coverage.sources is not deterministic")
    receipt_ids: set[str] = set()
    document_by_source = {row["source_id"]: row for row in rows}
    actual_counts = {status: 0 for status in sorted(_COVERAGE_STATUSES)}
    for index, value in enumerate(source_receipts):
        path = f"coverage.sources[{index}]"
        row = _exact_fields(value, _COVERAGE_SOURCE_FIELDS, path)
        source_id = _identifier(row["source_id"], f"{path}.source_id")
        if source_id in receipt_ids:
            raise PrimaryDocumentError("duplicate source coverage receipt")
        receipt_ids.add(source_id)
        if row["status"] not in _COVERAGE_STATUSES:
            raise PrimaryDocumentError(f"{path}.status is invalid")
        actual_counts[row["status"]] += 1
        _timestamp(row["attempted_at"], f"{path}.attempted_at")
        if type(row["document_available"]) is not bool:
            raise PrimaryDocumentError(f"{path}.document_available is invalid")
        if type(row["retained_last_good"]) is not bool:
            raise PrimaryDocumentError(f"{path}.retained_last_good is invalid")
        _text(row["reason"], f"{path}.reason", maximum=500)
        retained = document_by_source.get(source_id)
        if row["document_available"] != (retained is not None):
            raise PrimaryDocumentError(f"{path} availability does not match documents")
        expected_retained = retained is not None and row["status"] in {
            "fetch_error",
            "invalid_document",
            "store_error",
        }
        if row["retained_last_good"] != expected_retained:
            raise PrimaryDocumentError(f"{path} retained flag does not match status")
        expected_values = (
            (
                retained["document_id"],
                retained["current_vintage"]["vintage_id"],
                retained["current_vintage"]["content_sha256"],
            )
            if retained
            else (None, None, None)
        )
        if (row["document_id"], row["vintage_id"], row["content_sha256"]) != expected_values:
            raise PrimaryDocumentError(f"{path} identity does not match retained document")
    if counts != actual_counts:
        raise PrimaryDocumentError("coverage counts do not match source receipts")
    if coverage["attempted_sources"] != len(source_receipts) or sum(counts.values()) != len(
        source_receipts
    ):
        raise PrimaryDocumentError("coverage does not account for every source")
    successful = counts["captured"] + counts["unchanged"]
    if coverage["successful_sources"] != successful:
        raise PrimaryDocumentError("coverage successful count does not match statuses")
    expected_health = "healthy" if successful == coverage["registered_sources"] else "degraded"
    if coverage["status"] != expected_health:
        raise PrimaryDocumentError("coverage health does not match source statuses")
    if registry is not None and (
        coverage["registered_sources"] != len(registry.sources)
        or coverage["attempted_sources"] != len(registry.sources)
        or receipt_ids != set(sources_by_id)
    ):
        raise PrimaryDocumentError("coverage does not match the loaded registry")


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_OUTPUT_PATH",
    "INDEX_SCHEMA_VERSION",
    "InvalidPrimaryDocument",
    "PrimaryDocumentError",
    "PrimaryDocumentRegistryError",
    "PrimarySourceRegistry",
    "PrimarySourceSpec",
    "canonical_json_bytes",
    "collect_primary_documents",
    "format_timestamp",
    "load_primary_source_registry",
    "strict_json_loads",
    "validate_primary_document_index",
]
