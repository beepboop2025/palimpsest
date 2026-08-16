"""Publication-safe observations from explicitly configured social sources.

This module is the transport-neutral boundary between platform adapters and the
public Palimpsest evidence graph.  Adapters may use a platform-native identifier
to establish stable identity, but the identifier is reduced to an opaque digest
and is never retained in a public observation or version row.

Social observations are attributed reports, not corroboration.  A later
situation builder may match ``related_urls`` to RSS items or Observatory
measurements, but it must make that relationship outside this source artifact.

The implementation is deliberately standard-library only and deterministic for
a fixed registry, input records, prior state, and collection timestamp.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "social_sources.json"
DEFAULT_SOURCE_REGISTRY_URL = "https://palimpsest.info/config/social_sources.json"

REGISTRY_SCHEMA_VERSION = "palimpsest-social-sources.v1"
LATEST_SCHEMA_VERSION = "palimpsest-social-observations.v1"
LEDGER_SCHEMA_VERSION = "palimpsest-social-observation-version.v1"
SCHEMA_VERSION = LATEST_SCHEMA_VERSION
SCOPE = "bounded-registry-not-global"
RELATION = "attributed-source-report-not-corroboration"
RIGHTS_POLICY = "metadata-bounded-excerpt-link-only"
COLLECTION_POLICY = "public-or-operator-authorized"

MAX_TITLE_CHARS = 240
MAX_EXCERPT_CHARS = 640
MAX_NATIVE_ID_CHARS = 512
MAX_URL_CHARS = 2_048
MAX_RELATED_URLS = 16
MAX_LABELS = 12
MAX_ARTICLE_HOSTS = 64
_SAFE_INTEGER = 9_007_199_254_740_991

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_OBSERVATION_ID_RE = re.compile(r"^social-[0-9a-f]{32}$")
_VERSION_ID_RE = re.compile(r"^socialv-[0-9a-f]{32}$")
_TELEGRAM_PATH_RE = re.compile(r"^/([A-Za-z0-9_]{1,64})/([1-9][0-9]{0,19})/?$")
_INSTAGRAM_PATH_RE = re.compile(r"^/(p|reel|tv)/([A-Za-z0-9_-]{1,128})/?$")

_SOURCE_TYPE_PLATFORM = {
    "telegram_channel": "telegram",
    "instagram_professional": "instagram",
    "instagram_hashtag": "instagram",
}
_CONTENT_TYPES = {
    "text",
    "link",
    "image",
    "video",
    "audio",
    "document",
    "carousel",
    "other",
    "unavailable",
}
_STATES = {"published", "edited", "tombstone"}
_RECEIPT_STATUSES = {"success", "failure", "not-attempted"}
_TRACKING_QUERY_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "igsh",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "source",
        "spm",
    }
)
_CREDENTIAL_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
        "x-amz-credential",
        "x-amz-signature",
    }
)
_FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "access_token",
        "binary",
        "chat_id",
        "comment",
        "comments",
        "corroboration",
        "credential",
        "credentials",
        "dm",
        "dms",
        "engagement",
        "file_bytes",
        "like_count",
        "likes",
        "location",
        "media",
        "media_binary",
        "media_id",
        "message_text",
        "native_id",
        "native_ids",
        "password",
        "payload",
        "raw",
        "raw_payload",
        "related_event_ids",
        "secret",
        "share_count",
        "shares",
        "token",
        "view_count",
        "views",
    }
)

_REGISTRY_FIELDS = frozenset({"schema_version", "scope", "relation", "sources"})
_SOURCE_FIELDS = frozenset(
    {
        "id",
        "name",
        "source_type",
        "platform",
        "independence_group",
        "article_hosts",
        "collection_policy",
        "rights_policy",
    }
)
_ADAPTER_FIELDS = frozenset(
    {
        "source_id",
        "native_id",
        "permalink",
        "published_at",
        "observed_at",
        "title",
        "excerpt",
        "content_type",
        "content_sha256",
        "state",
        "china_relevance_labels",
        "related_urls",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "observation_id",
        "version_id",
        "supersedes_version_id",
        "platform",
        "source_id",
        "source_name",
        "source_type",
        "independence_group",
        "relation",
        "rights_policy",
        "permalink",
        "published_at",
        "first_observed_at",
        "title",
        "excerpt",
        "content_type",
        "content_sha256",
        "state",
        "china_relevance_labels",
        "related_urls",
    }
)
_VERSION_PAYLOAD_FIELDS = tuple(
    sorted(_OBSERVATION_FIELDS - {"version_id", "first_observed_at"})
)
_REVISION_CONTENT_FIELDS = tuple(
    sorted(
        _OBSERVATION_FIELDS
        - {"version_id", "supersedes_version_id", "first_observed_at"}
    )
)
_LATEST_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "source_registry",
        "source_registry_sha256",
        "scope",
        "relation",
        "coverage",
        "n_observations",
        "observations",
    }
)
_COVERAGE_FIELDS = frozenset(
    {"scope", "configured", "successful", "failed", "rejected", "receipts"}
)
_RECEIPT_FIELDS = frozenset(
    {"source_id", "platform", "status", "accepted", "rejected", "error_code"}
)
_INPUT_RECEIPT_FIELDS = frozenset({"source_id", "status", "rejected", "error_code"})
_LEDGER_FIELDS = frozenset({"schema_version", *_OBSERVATION_FIELDS})


class SocialObservationError(ValueError):
    """A source registry, adapter record, or public artifact is invalid."""


class SocialRegistryError(SocialObservationError):
    """The closed social-source registry is malformed or ambiguous."""


@dataclass(frozen=True)
class SocialSourceSpec:
    id: str
    name: str
    source_type: str
    platform: str
    independence_group: str
    article_hosts: tuple[str, ...]
    collection_policy: str
    rights_policy: str


@dataclass(frozen=True)
class SocialSourceRegistry:
    schema_version: str
    scope: str
    relation: str
    sources: tuple[SocialSourceSpec, ...]
    sha256: str

    def source(self, source_id: str) -> SocialSourceSpec:
        for item in self.sources:
            if item.id == source_id:
                return item
        raise SocialRegistryError(f"source is not in the closed registry: {source_id}")


def _reject_constant(value: str) -> None:
    raise SocialObservationError(f"non-finite JSON number is forbidden: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SocialObservationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: str | bytes, *, label: str = "JSON") -> Any:
    """Parse strict UTF-8 JSON while rejecting duplicate keys and NaN values."""

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "strict")
        return json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SocialObservationError(f"{label} is not strict UTF-8 JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize canonical JSON after rejecting non-public JSON extensions."""

    def reject(node: Any, path: str = "document") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise SocialObservationError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise SocialObservationError(f"{path} contains a non-string key")
                reject(child, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for index, child in enumerate(node):
                reject(child, f"{path}[{index}]")

    reject(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _exact_fields(value: Any, expected: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise SocialObservationError(f"{path} must be an object")
    actual = set(value)
    if actual != expected:
        raise SocialObservationError(
            f"{path} fields do not match contract "
            f"(missing={sorted(expected - actual)}, unknown={sorted(actual - expected)})"
        )
    return value


def _safe_text(
    value: Any,
    path: str,
    *,
    maximum: int,
    allow_empty: bool = False,
    collapse_whitespace: bool = False,
) -> str:
    if type(value) is not str:
        raise SocialObservationError(f"{path} must be a string")
    value = unicodedata.normalize("NFC", value)
    if collapse_whitespace:
        value = " ".join(value.split())
    if len(value) > maximum or (not allow_empty and not value.strip()):
        raise SocialObservationError(f"{path} is not bounded text")
    for character in value:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise SocialObservationError(f"{path} contains unsafe Unicode")
    return value


def _identifier(value: Any, path: str) -> str:
    text = _safe_text(value, path, maximum=80)
    if not _ID_RE.fullmatch(text):
        raise SocialObservationError(f"{path} is not a safe identifier")
    return text


def _count(value: Any, path: str) -> int:
    if type(value) is not int or not 0 <= value <= _SAFE_INTEGER:
        raise SocialObservationError(f"{path} must be a non-negative safe integer")
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TIMESTAMP_RE.fullmatch(value):
        raise SocialObservationError(f"{path} must be a canonical UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SocialObservationError(f"{path} is not a real timestamp") from exc
    return value


def _timestamp_value(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _sha256(value: Any, path: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise SocialObservationError(f"{path} must be a lowercase SHA-256 digest")
    return value


def _host(value: Any, path: str) -> str:
    host = _safe_text(value, path, maximum=253).lower()
    if (
        host != value
        or not _HOST_RE.fullmatch(host)
        or host == "palimpsest.info"
        or host.endswith(".palimpsest.info")
    ):
        raise SocialRegistryError(
            f"{path} must be an exact lowercase public DNS hostname"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise SocialRegistryError(f"{path} must not be an IP address")


def load_source_registry(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> SocialSourceRegistry:
    """Load the explicit allowlist; an explicitly empty list is a safe registry."""

    data = strict_json_loads(Path(path).read_bytes(), label="social source registry")
    top = _exact_fields(data, _REGISTRY_FIELDS, "registry")
    if top["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise SocialRegistryError("unsupported social source registry version")
    if top["scope"] != SCOPE or top["relation"] != RELATION:
        raise SocialRegistryError(
            "social source registry broadens the v1 evidence boundary"
        )
    rows = top["sources"]
    if type(rows) is not list or len(rows) > 256:
        raise SocialRegistryError("registry.sources must be a bounded explicit array")

    sources: list[SocialSourceSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        row = _exact_fields(raw, _SOURCE_FIELDS, f"sources[{index}]")
        source_id = _identifier(row["id"], f"sources[{index}].id")
        if source_id in seen:
            raise SocialRegistryError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        source_type = _safe_text(
            row["source_type"], f"sources[{index}].source_type", maximum=40
        )
        if source_type not in _SOURCE_TYPE_PLATFORM:
            raise SocialRegistryError(f"sources[{index}].source_type is unsupported")
        expected_platform = _SOURCE_TYPE_PLATFORM[source_type]
        if row["platform"] != expected_platform:
            raise SocialRegistryError(
                f"sources[{index}].platform does not match source_type"
            )
        raw_hosts = row["article_hosts"]
        if type(raw_hosts) is not list or len(raw_hosts) > MAX_ARTICLE_HOSTS:
            raise SocialRegistryError(
                f"sources[{index}].article_hosts must be a bounded array"
            )
        article_hosts = tuple(
            _host(value, f"sources[{index}].article_hosts[{host_index}]")
            for host_index, value in enumerate(raw_hosts)
        )
        if list(article_hosts) != sorted(set(article_hosts)):
            raise SocialRegistryError(
                f"sources[{index}].article_hosts must be sorted and unique"
            )
        if row["collection_policy"] != COLLECTION_POLICY:
            raise SocialRegistryError(
                f"sources[{index}] changes the collection authorization boundary"
            )
        if row["rights_policy"] != RIGHTS_POLICY:
            raise SocialRegistryError(
                f"sources[{index}] changes the publication rights boundary"
            )
        sources.append(
            SocialSourceSpec(
                id=source_id,
                name=_safe_text(row["name"], f"sources[{index}].name", maximum=120),
                source_type=source_type,
                platform=expected_platform,
                independence_group=_identifier(
                    row["independence_group"], f"sources[{index}].independence_group"
                ),
                article_hosts=article_hosts,
                collection_policy=COLLECTION_POLICY,
                rights_policy=RIGHTS_POLICY,
            )
        )
    if [source.id for source in sources] != sorted(seen):
        raise SocialRegistryError("registry.sources must be sorted by id")

    return SocialSourceRegistry(
        schema_version=REGISTRY_SCHEMA_VERSION,
        scope=SCOPE,
        relation=RELATION,
        sources=tuple(sources),
        sha256=hashlib.sha256(canonical_json_bytes(data)).hexdigest(),
    )


def _registry_document_for_sources(
    registry: SocialSourceRegistry, sources: Sequence[SocialSourceSpec]
) -> dict[str, Any]:
    return {
        "schema_version": registry.schema_version,
        "scope": registry.scope,
        "relation": registry.relation,
        "sources": [
            {
                "id": source.id,
                "name": source.name,
                "source_type": source.source_type,
                "platform": source.platform,
                "independence_group": source.independence_group,
                "article_hosts": list(source.article_hosts),
                "collection_policy": source.collection_policy,
                "rights_policy": source.rights_policy,
            }
            for source in sources
        ],
    }


def migrate_latest_registry_additions(
    document: Mapping[str, Any],
    registry: SocialSourceRegistry | None = None,
) -> dict[str, Any]:
    """Migrate a latest view only across a proven additive registry change.

    The prior artifact supplies its old source set through coverage receipts. We
    reconstruct that subset from the current locked metadata and require its
    canonical digest to equal the artifact's prior registry digest. This proves
    that retained source metadata did not change while allowing only new rows.
    """

    registry = registry or load_source_registry()
    _scan_forbidden_fields(document, "social_observations")
    top = _exact_fields(document, _LATEST_FIELDS, "social_observations")
    coverage = _exact_fields(top["coverage"], _COVERAGE_FIELDS, "coverage")
    receipts = coverage["receipts"]
    if type(receipts) is not list or len(receipts) > 256:
        raise SocialObservationError("prior coverage receipts must be a bounded array")
    prior_ids: set[str] = set()
    for index, raw_receipt in enumerate(receipts):
        receipt = _exact_fields(
            raw_receipt, _RECEIPT_FIELDS, f"coverage.receipts[{index}]"
        )
        source_id = _identifier(
            receipt["source_id"], f"coverage.receipts[{index}].source_id"
        )
        if source_id in prior_ids:
            raise SocialObservationError("prior coverage contains duplicate sources")
        prior_ids.add(source_id)

    current_ids = {source.id for source in registry.sources}
    if not prior_ids < current_ids:
        raise SocialObservationError(
            "source registry change is not a strict additive extension"
        )
    subset_sources = tuple(
        source for source in registry.sources if source.id in prior_ids
    )
    subset_document = _registry_document_for_sources(registry, subset_sources)
    subset_digest = hashlib.sha256(canonical_json_bytes(subset_document)).hexdigest()
    if top["source_registry_sha256"] != subset_digest:
        raise SocialObservationError(
            "retained source metadata changed across registry migration"
        )
    prior_registry = SocialSourceRegistry(
        schema_version=registry.schema_version,
        scope=registry.scope,
        relation=registry.relation,
        sources=subset_sources,
        sha256=subset_digest,
    )
    validate_latest(document, prior_registry)

    receipt_by_source = {receipt["source_id"]: dict(receipt) for receipt in receipts}
    migrated_receipts = []
    for source in registry.sources:
        migrated_receipts.append(
            receipt_by_source.get(source.id)
            or {
                "source_id": source.id,
                "platform": source.platform,
                "status": "not-attempted",
                "accepted": 0,
                "rejected": 0,
                "error_code": None,
            }
        )
    migrated = {
        **document,
        "source_registry_sha256": registry.sha256,
        "coverage": {
            **coverage,
            "configured": len(registry.sources),
            "receipts": migrated_receipts,
        },
    }
    validate_latest(migrated, registry)
    return migrated


def _canonical_platform_permalink(value: Any, source: SocialSourceSpec) -> str:
    text = _safe_text(value, "record.permalink", maximum=MAX_URL_CHARS)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise SocialObservationError("record.permalink is not a valid URL") from exc
    if (
        parts.scheme.casefold() != "https"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.query
        or parts.fragment
    ):
        raise SocialObservationError(
            "record.permalink must be credential-free canonical HTTPS"
        )
    host = (parts.hostname or "").lower().rstrip(".")
    if source.platform == "telegram":
        match = _TELEGRAM_PATH_RE.fullmatch(parts.path)
        if (
            host != "t.me"
            or match is None
            or match.group(1).casefold() in {"c", "s", "joinchat"}
        ):
            raise SocialObservationError(
                "record.permalink is not a public Telegram channel post"
            )
        return f"https://t.me/{match.group(1).casefold()}/{match.group(2)}/"
    match = _INSTAGRAM_PATH_RE.fullmatch(parts.path)
    if host not in {"instagram.com", "www.instagram.com"} or match is None:
        raise SocialObservationError(
            "record.permalink is not a canonical Instagram post"
        )
    return f"https://www.instagram.com/{match.group(1)}/{match.group(2)}/"


def _canonical_related_url(value: Any, source: SocialSourceSpec, path: str) -> str:
    text = _safe_text(value, path, maximum=MAX_URL_CHARS)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise SocialObservationError(f"{path} is not a valid URL") from exc
    host = (parts.hostname or "").lower().rstrip(".")
    if (
        parts.scheme.casefold() != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or host not in source.article_hosts
        or not parts.path.startswith("/")
        or len(parts.path) > 1_500
    ):
        raise SocialObservationError(
            f"{path} is outside the exact credential-free HTTPS host allowlist"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SocialObservationError(
            f"{path} must use an allowlisted public DNS hostname"
        )
    try:
        pairs = parse_qsl(
            parts.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=64,
        )
    except ValueError as exc:
        raise SocialObservationError(f"{path} query exceeds the field cap") from exc
    kept: list[tuple[str, str]] = []
    for key, item in pairs:
        folded = key.casefold()
        if folded in _CREDENTIAL_QUERY_NAMES:
            raise SocialObservationError(
                f"{path} contains a credential-bearing query field"
            )
        if folded.startswith("utm_") or folded in _TRACKING_QUERY_NAMES:
            continue
        kept.append((key, item))
    return urlunsplit(
        ("https", host, parts.path or "/", urlencode(sorted(kept), doseq=True), "")
    )


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:32]}"


def _version_id_for(observation: Mapping[str, Any]) -> str:
    return _stable_id(
        "socialv", {field: observation[field] for field in _VERSION_PAYLOAD_FIELDS}
    )


def _revision_content(observation: Mapping[str, Any]) -> dict[str, Any]:
    return {field: observation[field] for field in _REVISION_CONTENT_FIELDS}


def _instagram_content_without_state(observation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: observation[field]
        for field in _REVISION_CONTENT_FIELDS
        if field != "state"
    }


def _normalize_string_array(
    value: Any,
    path: str,
    *,
    maximum: int,
    allow_empty: bool,
) -> list[str]:
    if (
        type(value) is not list
        or len(value) > maximum
        or (not allow_empty and not value)
    ):
        raise SocialObservationError(f"{path} must be a bounded array")
    result = [_identifier(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise SocialObservationError(f"{path} contains duplicates")
    return sorted(result)


def normalize_record(
    record: Mapping[str, Any], registry: SocialSourceRegistry | None = None
) -> dict[str, Any]:
    """Normalize one adapter record without retaining its native identifier.

    ``native_id`` exists only at this ingestion boundary.  It participates in the
    opaque observation digest and is then discarded before this function returns.
    Every other adapter field is already publication-intended metadata.
    """

    registry = registry or load_source_registry()
    row = _exact_fields(record, _ADAPTER_FIELDS, "record")
    source_id = _identifier(row["source_id"], "record.source_id")
    source = registry.source(source_id)
    native_id = _safe_text(
        row["native_id"], "record.native_id", maximum=MAX_NATIVE_ID_CHARS
    )
    published_at = _timestamp(row["published_at"], "record.published_at")
    observed_at = _timestamp(row["observed_at"], "record.observed_at")
    if _timestamp_value(published_at) > _timestamp_value(observed_at):
        raise SocialObservationError(
            "record.published_at is later than record.observed_at"
        )
    state = row["state"]
    content_type = row["content_type"]
    if state not in _STATES or content_type not in _CONTENT_TYPES:
        raise SocialObservationError("record state/content_type is unsupported")
    title = _safe_text(
        row["title"],
        "record.title",
        maximum=MAX_TITLE_CHARS,
        allow_empty=state == "tombstone",
        collapse_whitespace=True,
    )
    excerpt = _safe_text(
        row["excerpt"],
        "record.excerpt",
        maximum=MAX_EXCERPT_CHARS,
        allow_empty=True,
        collapse_whitespace=True,
    )
    raw_urls = row["related_urls"]
    if type(raw_urls) is not list or len(raw_urls) > MAX_RELATED_URLS:
        raise SocialObservationError("record.related_urls must be a bounded array")
    related_urls = sorted(
        {
            _canonical_related_url(value, source, f"record.related_urls[{index}]")
            for index, value in enumerate(raw_urls)
        }
    )
    if state == "tombstone":
        if title or excerpt or content_type != "unavailable" or related_urls:
            raise SocialObservationError(
                "tombstones must not retain removed publication content"
            )
    elif not title or content_type == "unavailable":
        raise SocialObservationError(
            "published/edited observations require a title and content type"
        )

    observation_id = _stable_id(
        "social",
        {"platform": source.platform, "source_id": source.id, "native_id": native_id},
    )
    normalized: dict[str, Any] = {
        "observation_id": observation_id,
        "version_id": "",
        "supersedes_version_id": None,
        "platform": source.platform,
        "source_id": source.id,
        "source_name": source.name,
        "source_type": source.source_type,
        "independence_group": source.independence_group,
        "relation": RELATION,
        "rights_policy": source.rights_policy,
        "permalink": _canonical_platform_permalink(row["permalink"], source),
        "published_at": published_at,
        "first_observed_at": observed_at,
        "title": title,
        "excerpt": excerpt,
        "content_type": content_type,
        "content_sha256": _sha256(row["content_sha256"], "record.content_sha256"),
        "state": state,
        "china_relevance_labels": _normalize_string_array(
            row["china_relevance_labels"],
            "record.china_relevance_labels",
            maximum=MAX_LABELS,
            allow_empty=False,
        ),
        "related_urls": related_urls,
    }
    normalized["version_id"] = _version_id_for(normalized)
    _validate_observation(normalized, registry, "record.normalized")
    return normalized


def _scan_forbidden_fields(node: Any, path: str) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if str(key).casefold() in _FORBIDDEN_PUBLIC_FIELDS:
                raise SocialObservationError(
                    f"{path} contains forbidden public field {key!r}"
                )
            _scan_forbidden_fields(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_forbidden_fields(value, f"{path}[{index}]")


def _validate_observation(
    value: Any,
    registry: SocialSourceRegistry,
    path: str,
) -> Mapping[str, Any]:
    observation = _exact_fields(value, _OBSERVATION_FIELDS, path)
    if type(
        observation["observation_id"]
    ) is not str or not _OBSERVATION_ID_RE.fullmatch(observation["observation_id"]):
        raise SocialObservationError(f"{path}.observation_id is invalid")
    if type(observation["version_id"]) is not str or not _VERSION_ID_RE.fullmatch(
        observation["version_id"]
    ):
        raise SocialObservationError(f"{path}.version_id is invalid")
    previous = observation["supersedes_version_id"]
    if previous is not None and (
        type(previous) is not str or not _VERSION_ID_RE.fullmatch(previous)
    ):
        raise SocialObservationError(f"{path}.supersedes_version_id is invalid")
    source_id = _identifier(observation["source_id"], f"{path}.source_id")
    source = registry.source(source_id)
    expected_source_fields = {
        "platform": source.platform,
        "source_name": source.name,
        "source_type": source.source_type,
        "independence_group": source.independence_group,
        "rights_policy": source.rights_policy,
    }
    if any(
        observation[field] != expected
        for field, expected in expected_source_fields.items()
    ):
        raise SocialObservationError(f"{path} changes locked source metadata")
    if observation["relation"] != RELATION:
        raise SocialObservationError(f"{path}.relation must remain non-corroborating")
    published_at = _timestamp(observation["published_at"], f"{path}.published_at")
    observed_at = _timestamp(
        observation["first_observed_at"], f"{path}.first_observed_at"
    )
    if _timestamp_value(published_at) > _timestamp_value(observed_at):
        raise SocialObservationError(f"{path} was observed before it was published")
    if observation["permalink"] != _canonical_platform_permalink(
        observation["permalink"], source
    ):
        raise SocialObservationError(f"{path}.permalink is not canonical")
    state = observation["state"]
    content_type = observation["content_type"]
    if state not in _STATES or content_type not in _CONTENT_TYPES:
        raise SocialObservationError(f"{path} state/content_type is unsupported")
    title = _safe_text(
        observation["title"],
        f"{path}.title",
        maximum=MAX_TITLE_CHARS,
        allow_empty=state == "tombstone",
    )
    excerpt = _safe_text(
        observation["excerpt"],
        f"{path}.excerpt",
        maximum=MAX_EXCERPT_CHARS,
        allow_empty=True,
    )
    _sha256(observation["content_sha256"], f"{path}.content_sha256")
    labels = _normalize_string_array(
        observation["china_relevance_labels"],
        f"{path}.china_relevance_labels",
        maximum=MAX_LABELS,
        allow_empty=False,
    )
    if labels != observation["china_relevance_labels"]:
        raise SocialObservationError(f"{path}.china_relevance_labels is not canonical")
    urls = observation["related_urls"]
    if type(urls) is not list or len(urls) > MAX_RELATED_URLS:
        raise SocialObservationError(f"{path}.related_urls must be a bounded array")
    normalized_urls = [
        _canonical_related_url(url, source, f"{path}.related_urls[{index}]")
        for index, url in enumerate(urls)
    ]
    if urls != sorted(set(normalized_urls)):
        raise SocialObservationError(
            f"{path}.related_urls is not canonical, sorted, and unique"
        )
    if state == "tombstone":
        if title or excerpt or content_type != "unavailable" or urls:
            raise SocialObservationError(
                f"{path} tombstone retains removed publication content"
            )
    elif not title or content_type == "unavailable":
        raise SocialObservationError(f"{path} published content is incomplete")
    expected_version = _version_id_for(observation)
    if observation["version_id"] != expected_version:
        raise SocialObservationError(
            f"{path}.version_id does not match sanitized metadata"
        )
    return observation


def _validate_coverage(
    coverage: Any,
    registry: SocialSourceRegistry,
) -> None:
    row = _exact_fields(coverage, _COVERAGE_FIELDS, "coverage")
    if row["scope"] != SCOPE:
        raise SocialObservationError(
            "coverage.scope must disclose the bounded registry"
        )
    for field in ("configured", "successful", "failed", "rejected"):
        _count(row[field], f"coverage.{field}")
    if row["configured"] != len(registry.sources):
        raise SocialObservationError("coverage.configured does not match the registry")
    receipts = row["receipts"]
    if type(receipts) is not list or len(receipts) != len(registry.sources):
        raise SocialObservationError(
            "coverage.receipts does not account for every configured source"
        )
    seen: set[str] = set()
    successful = 0
    failed = 0
    rejected = 0
    for index, raw_receipt in enumerate(receipts):
        receipt = _exact_fields(
            raw_receipt, _RECEIPT_FIELDS, f"coverage.receipts[{index}]"
        )
        source_id = _identifier(
            receipt["source_id"], f"coverage.receipts[{index}].source_id"
        )
        if source_id in seen:
            raise SocialObservationError("coverage contains duplicate source receipts")
        seen.add(source_id)
        source = registry.source(source_id)
        if (
            receipt["platform"] != source.platform
            or receipt["status"] not in _RECEIPT_STATUSES
        ):
            raise SocialObservationError(
                "coverage receipt changes platform/status semantics"
            )
        _count(receipt["accepted"], f"coverage.receipts[{index}].accepted")
        rejected += _count(receipt["rejected"], f"coverage.receipts[{index}].rejected")
        error_code = receipt["error_code"]
        if receipt["status"] == "failure":
            _identifier(error_code, f"coverage.receipts[{index}].error_code")
            if receipt["accepted"]:
                raise SocialObservationError(
                    "failed source receipt cannot claim accepted records"
                )
            failed += 1
        else:
            if error_code is not None:
                raise SocialObservationError(
                    "non-failure source receipt must not carry an error code"
                )
            if receipt["status"] == "not-attempted" and receipt["accepted"]:
                raise SocialObservationError(
                    "unattempted source receipt cannot claim accepted records"
                )
            successful += int(receipt["status"] == "success")
    if [receipt["source_id"] for receipt in receipts] != sorted(seen):
        raise SocialObservationError("coverage receipts must be sorted by source_id")
    if seen != {source.id for source in registry.sources}:
        raise SocialObservationError(
            "coverage receipts do not match the closed registry"
        )
    if (row["successful"], row["failed"], row["rejected"]) != (
        successful,
        failed,
        rejected,
    ):
        raise SocialObservationError(
            "coverage summary counts do not match source receipts"
        )


def validate_latest(
    document: Mapping[str, Any], registry: SocialSourceRegistry | None = None
) -> None:
    """Validate a complete latest view and all publication-safety invariants."""

    registry = registry or load_source_registry()
    _scan_forbidden_fields(document, "social_observations")
    top = _exact_fields(document, _LATEST_FIELDS, "social_observations")
    if top["schema_version"] != LATEST_SCHEMA_VERSION:
        raise SocialObservationError("unsupported social observation schema version")
    generated_at = _timestamp(top["generated_at"], "generated_at")
    if top["source_registry"] != DEFAULT_SOURCE_REGISTRY_URL:
        raise SocialObservationError("source_registry URL is not canonical")
    if top["source_registry_sha256"] != registry.sha256:
        raise SocialObservationError(
            "source registry digest does not match the closed registry"
        )
    if top["scope"] != SCOPE or top["relation"] != RELATION:
        raise SocialObservationError(
            "latest view broadens the social evidence boundary"
        )
    _validate_coverage(top["coverage"], registry)
    observations = top["observations"]
    if type(observations) is not list or top["n_observations"] != len(observations):
        raise SocialObservationError("n_observations does not match observations")
    _count(top["n_observations"], "n_observations")
    seen_observations: set[str] = set()
    seen_versions: set[str] = set()
    for index, observation in enumerate(observations):
        validated = _validate_observation(
            observation, registry, f"observations[{index}]"
        )
        if validated["observation_id"] in seen_observations:
            raise SocialObservationError(
                "latest view contains duplicate observation_id"
            )
        if validated["version_id"] in seen_versions:
            raise SocialObservationError("latest view contains duplicate version_id")
        if _timestamp_value(validated["first_observed_at"]) > _timestamp_value(
            generated_at
        ):
            raise SocialObservationError(
                "latest view contains an observation from the future"
            )
        seen_observations.add(validated["observation_id"])
        seen_versions.add(validated["version_id"])
    expected_order = sorted(
        observations,
        key=lambda value: (
            -_timestamp_value(value["published_at"]).timestamp(),
            value["observation_id"],
        ),
    )
    if observations != expected_order:
        raise SocialObservationError(
            "observations are not in deterministic reverse-chronological order"
        )
    canonical_json_bytes(document)


def validate_ledger_row(
    row: Mapping[str, Any], registry: SocialSourceRegistry | None = None
) -> None:
    """Validate one immutable version row without assuming ledger position."""

    registry = registry or load_source_registry()
    _scan_forbidden_fields(row, "social_observation_version")
    value = _exact_fields(row, _LEDGER_FIELDS, "social_observation_version")
    if value["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise SocialObservationError(
            "unsupported social observation ledger schema version"
        )
    observation = {field: value[field] for field in _OBSERVATION_FIELDS}
    _validate_observation(observation, registry, "social_observation_version")
    canonical_json_bytes(row)


def validate_ledger_rows(
    rows: Sequence[Mapping[str, Any]], registry: SocialSourceRegistry | None = None
) -> None:
    """Validate an append-only ledger, including per-observation revision chains."""

    registry = registry or load_source_registry()
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise SocialObservationError("ledger rows must be a sequence")
    terminals: dict[str, Mapping[str, Any]] = {}
    version_ids: set[str] = set()
    for index, row in enumerate(rows):
        validate_ledger_row(row, registry)
        observation_id = row["observation_id"]
        version_id = row["version_id"]
        if version_id in version_ids:
            raise SocialObservationError(f"ledger row {index} duplicates a version_id")
        previous = terminals.get(observation_id)
        expected_previous = previous["version_id"] if previous else None
        if row["supersedes_version_id"] != expected_previous:
            raise SocialObservationError(
                f"ledger row {index} breaks its append-only revision chain"
            )
        if previous and _timestamp_value(row["first_observed_at"]) < _timestamp_value(
            previous["first_observed_at"]
        ):
            raise SocialObservationError(
                f"ledger row {index} moves first_observed_at backwards"
            )
        terminals[observation_id] = row
        version_ids.add(version_id)


def _input_receipts(
    collection_receipts: Sequence[Mapping[str, Any]] | None,
    registry: SocialSourceRegistry,
    accepted_by_source: Mapping[str, int],
) -> list[dict[str, Any]]:
    if collection_receipts is None:
        inputs: list[Mapping[str, Any]] = [
            {
                "source_id": source.id,
                "status": "success"
                if accepted_by_source.get(source.id, 0)
                else "not-attempted",
                "rejected": 0,
                "error_code": None,
            }
            for source in registry.sources
        ]
    else:
        if isinstance(collection_receipts, (str, bytes)) or not isinstance(
            collection_receipts, Sequence
        ):
            raise SocialObservationError("collection_receipts must be a sequence")
        inputs = list(collection_receipts)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(inputs):
        receipt = _exact_fields(
            raw, _INPUT_RECEIPT_FIELDS, f"collection_receipts[{index}]"
        )
        source_id = _identifier(
            receipt["source_id"], f"collection_receipts[{index}].source_id"
        )
        if source_id in seen:
            raise SocialObservationError(
                "collection_receipts contains duplicate sources"
            )
        seen.add(source_id)
        source = registry.source(source_id)
        status = receipt["status"]
        if status not in _RECEIPT_STATUSES:
            raise SocialObservationError("collection receipt status is invalid")
        rejected = _count(receipt["rejected"], f"collection_receipts[{index}].rejected")
        error_code = receipt["error_code"]
        if status == "failure":
            error_code = _identifier(
                error_code, f"collection_receipts[{index}].error_code"
            )
        elif error_code is not None:
            raise SocialObservationError(
                "only failed collection receipts may carry error_code"
            )
        accepted = accepted_by_source.get(source_id, 0)
        if status != "success" and accepted:
            raise SocialObservationError(
                "non-success collection receipt has accepted observations"
            )
        output.append(
            {
                "source_id": source.id,
                "platform": source.platform,
                "status": status,
                "accepted": accepted,
                "rejected": rejected,
                "error_code": error_code,
            }
        )
    expected = {source.id for source in registry.sources}
    if seen != expected:
        raise SocialObservationError(
            f"collection_receipts must cover the closed registry "
            f"(missing={sorted(expected - seen)}, unknown={sorted(seen - expected)})"
        )
    return sorted(output, key=lambda receipt: receipt["source_id"])


def _seed_prior_rows(
    prior_latest: Mapping[str, Any] | None,
    prior_ledger: Sequence[Mapping[str, Any]],
    registry: SocialSourceRegistry,
) -> list[dict[str, Any]]:
    ledger = [dict(row) for row in prior_ledger]
    validate_ledger_rows(ledger, registry)
    if prior_latest is None:
        if ledger:
            raise SocialObservationError(
                "prior_ledger requires its matching prior_latest view"
            )
        return ledger
    validate_latest(prior_latest, registry)
    if not ledger:
        for observation in sorted(
            prior_latest["observations"],
            key=lambda value: (value["first_observed_at"], value["observation_id"]),
        ):
            row = {"schema_version": LEDGER_SCHEMA_VERSION, **dict(observation)}
            row["supersedes_version_id"] = None
            row["version_id"] = _version_id_for(row)
            ledger.append(row)
        validate_ledger_rows(ledger, registry)
        return ledger
    terminals: dict[str, Mapping[str, Any]] = {}
    for row in ledger:
        terminals[row["observation_id"]] = row
    latest_by_id = {row["observation_id"]: row for row in prior_latest["observations"]}
    if set(terminals) != set(latest_by_id) or any(
        terminals[observation_id]["version_id"]
        != latest_by_id[observation_id]["version_id"]
        for observation_id in terminals
    ):
        raise SocialObservationError(
            "prior_latest does not match prior_ledger terminals"
        )
    return ledger


def build_latest(
    records: Iterable[Mapping[str, Any]],
    *,
    registry: SocialSourceRegistry | None = None,
    generated_at: str,
    prior_latest: Mapping[str, Any] | None = None,
    prior_ledger: Sequence[Mapping[str, Any]] = (),
    collection_receipts: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Build a latest view and its append-only JSONL version rows.

    ``collection_receipts`` must cover every configured source when supplied.
    This distinguishes a successful zero-result collection from a transport
    failure or a source that was intentionally not attempted.
    """

    registry = registry or load_source_registry()
    generated_at = _timestamp(generated_at, "generated_at")
    ledger = _seed_prior_rows(prior_latest, prior_ledger, registry)
    if prior_latest is not None and _timestamp_value(
        prior_latest["generated_at"]
    ) > _timestamp_value(generated_at):
        raise SocialObservationError("generated_at moves backwards from prior_latest")
    normalized = [normalize_record(record, registry) for record in records]
    for observation in normalized:
        if _timestamp_value(observation["first_observed_at"]) > _timestamp_value(
            generated_at
        ):
            raise SocialObservationError(
                "record.observed_at is later than generated_at"
            )

    by_observation: dict[str, list[dict[str, Any]]] = {}
    for observation in normalized:
        by_observation.setdefault(observation["observation_id"], []).append(observation)
    candidates: list[dict[str, Any]] = []
    for observation_id in sorted(by_observation):
        previous_content: dict[str, Any] | None = None
        ordered = sorted(
            by_observation[observation_id],
            key=lambda value: (value["first_observed_at"], value["version_id"]),
        )
        for observation in ordered:
            content = _revision_content(observation)
            if content == previous_content:
                continue
            candidates.append(observation)
            previous_content = content

    accepted_by_source: dict[str, int] = {}
    for observation in candidates:
        source_id = observation["source_id"]
        accepted_by_source[source_id] = accepted_by_source.get(source_id, 0) + 1
    receipts = _input_receipts(collection_receipts, registry, accepted_by_source)

    known_versions = {row["version_id"] for row in ledger}
    terminals: dict[str, dict[str, Any]] = {}
    for row in ledger:
        terminals[row["observation_id"]] = row
    candidates = sorted(
        candidates,
        key=lambda value: (
            value["first_observed_at"],
            value["observation_id"],
            value["version_id"],
        ),
    )
    for candidate in candidates:
        observation = dict(candidate)
        previous = terminals.get(observation["observation_id"])
        if previous and _timestamp_value(
            observation["first_observed_at"]
        ) < _timestamp_value(previous["first_observed_at"]):
            raise SocialObservationError(
                "new version predates its append-only ledger terminal"
            )
        if (
            previous
            and observation["platform"] == "instagram"
            and observation["state"] == "published"
            and previous["state"] in {"published", "edited"}
        ):
            if _instagram_content_without_state(
                observation
            ) == _instagram_content_without_state(previous):
                continue
            observation["state"] = "edited"
        if previous and _revision_content(observation) == _revision_content(previous):
            continue
        observation["supersedes_version_id"] = (
            previous["version_id"] if previous else None
        )
        observation["version_id"] = _version_id_for(observation)
        if observation["version_id"] in known_versions:
            raise SocialObservationError(
                "new revision collides with an existing version_id"
            )
        row = {"schema_version": LEDGER_SCHEMA_VERSION, **observation}
        validate_ledger_row(row, registry)
        ledger.append(row)
        terminals[observation["observation_id"]] = row
        known_versions.add(observation["version_id"])

    observations = [
        {field: row[field] for field in _OBSERVATION_FIELDS}
        for row in terminals.values()
    ]
    observations.sort(
        key=lambda value: (
            -_timestamp_value(value["published_at"]).timestamp(),
            value["observation_id"],
        )
    )
    coverage = {
        "scope": SCOPE,
        "configured": len(registry.sources),
        "successful": sum(receipt["status"] == "success" for receipt in receipts),
        "failed": sum(receipt["status"] == "failure" for receipt in receipts),
        "rejected": sum(receipt["rejected"] for receipt in receipts),
        "receipts": receipts,
    }
    latest: dict[str, Any] = {
        "schema_version": LATEST_SCHEMA_VERSION,
        "generated_at": generated_at,
        "source_registry": DEFAULT_SOURCE_REGISTRY_URL,
        "source_registry_sha256": registry.sha256,
        "scope": SCOPE,
        "relation": RELATION,
        "coverage": coverage,
        "n_observations": len(observations),
        "observations": observations,
    }
    validate_ledger_rows(ledger, registry)
    validate_latest(latest, registry)
    return latest, tuple(ledger)


build_latest_and_ledger = build_latest
normalize_adapter_record = normalize_record


def ledger_jsonl_bytes(
    rows: Sequence[Mapping[str, Any]], registry: SocialSourceRegistry | None = None
) -> bytes:
    """Render validated append-only version rows as canonical JSON Lines."""

    validate_ledger_rows(rows, registry)
    if not rows:
        return b""
    return b"\n".join(canonical_json_bytes(row) for row in rows) + b"\n"


def load_latest_document(
    path: Path | str,
    registry: SocialSourceRegistry | None = None,
) -> dict[str, Any]:
    """Load a latest artifact without permitting duplicate JSON keys."""

    value = strict_json_loads(
        Path(path).read_bytes(), label="social observations latest"
    )
    validate_latest(value, registry)
    return value


def load_ledger_jsonl(
    path: Path | str,
    registry: SocialSourceRegistry | None = None,
) -> tuple[dict[str, Any], ...]:
    """Load strict JSONL and validate every immutable row and revision edge."""

    raw = Path(path).read_bytes()
    if not raw:
        return ()
    if not raw.endswith(b"\n"):
        raise SocialObservationError(
            "social observation ledger must end with a newline"
        )
    lines = raw.splitlines()
    if any(not line.strip() for line in lines):
        raise SocialObservationError("social observation ledger contains a blank row")
    rows = tuple(
        strict_json_loads(line, label=f"social observation ledger row {index}")
        for index, line in enumerate(lines)
    )
    validate_ledger_rows(rows, registry)
    return rows


__all__ = [
    "COLLECTION_POLICY",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_SOURCE_REGISTRY_URL",
    "LATEST_SCHEMA_VERSION",
    "LEDGER_SCHEMA_VERSION",
    "REGISTRY_SCHEMA_VERSION",
    "RELATION",
    "RIGHTS_POLICY",
    "SCHEMA_VERSION",
    "SCOPE",
    "SocialObservationError",
    "SocialRegistryError",
    "SocialSourceRegistry",
    "SocialSourceSpec",
    "build_latest",
    "build_latest_and_ledger",
    "canonical_json_bytes",
    "ledger_jsonl_bytes",
    "load_latest_document",
    "load_ledger_jsonl",
    "load_source_registry",
    "migrate_latest_registry_additions",
    "normalize_adapter_record",
    "normalize_record",
    "strict_json_loads",
    "validate_latest",
    "validate_ledger_row",
    "validate_ledger_rows",
]
