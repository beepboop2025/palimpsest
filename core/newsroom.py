"""Deterministic, aggregate-only newsroom transform for the China OSINT roll-up.

The newsroom is deliberately a small stdlib boundary.  It turns the operational
``osint-china.v1`` board into an editorial feed without copying any signal payloads.
That omission is a safety property: payloads can contain excerpts, repository rows,
or other source-specific records, while the public news contract contains aggregate
claims and their provenance only.

Availability is also a claim boundary.  A missing, stale, degraded, or corrupt
signal still receives a story so readers can see the gap, but it never receives a
finding and its possibly-retained metric is not republished as current.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import string
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_PATH = ROOT / "readings" / "osint-china-latest.json"
DEFAULT_CONFIG_PATH = ROOT / "config" / "newsroom.json"
DEFAULT_OUTPUT_PATH = ROOT / "readings" / "newsroom-latest.json"
SCHEMA_PATH = ROOT / "protocol" / "news-feed-v1.schema.json"

SOURCE_SCHEMA_VERSION = "osint-china.v1"
CONFIG_SCHEMA_VERSION = "palimpsest-newsroom-config.v1"
NEWS_SCHEMA_VERSION = "palimpsest-news.v1"
FEED_ID = "palimpsest-china-newsroom"
FEED_URL = "https://palimpsest.info/news/"
SOURCE_URL = "https://palimpsest.info/readings/osint-china-latest.json"
NEWS_METHOD = (
    "Deterministic aggregate-only editorial transform of the osint-china.v1 roll-up; "
    "no signal payload rows or person-level data are copied into this feed."
)

_SAFE_INTEGER = 9_007_199_254_740_991
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,126}\.json$")
_BELIEVABILITY_MIN_HISTORY = 8
_UNSAFE_TEXT_RE = re.compile(
    r"(?:<\s*/?\s*(?:script|iframe|object|embed|style|svg)\b|"
    r"javascript\s*:|data\s*:\s*text/html|on(?:error|load|click)\s*=)",
    re.IGNORECASE,
)
_TEMPLATE_FIELDS = frozenset(
    {
        "denominator",
        "denominator_label",
        "board_headline",
        "board_story_headline",
        "metric_label",
        "signal_name",
        "status",
        "unit",
        "value",
        "value_percent",
    }
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "alerts",
        "generated_at",
        "headline",
        "health",
        "input_commit",
        "layers",
        "method",
        "method_version",
        "n_signals_live",
        "n_signals_reporting",
        "n_signals_total",
        "schema_version",
        "scope",
        "signals",
        "source",
    }
)
_SIGNAL_FIELDS = frozenset(
    {
        "cadence_hours",
        "freshness_deadline",
        "health",
        "id",
        "input",
        "layer",
        "live",
        "method",
        "method_version",
        "metric",
        "optional",
        "payload",
        "payload_complete",
        "raw_url",
        "scope",
        "source",
        "source_timestamp",
        "status",
        "summary",
        "title",
    }
)
_SIGNAL_HEALTH_FIELDS = frozenset(
    {
        "age_hours",
        "collector_reason",
        "collector_status",
        "ok",
        "pipeline_checked_at",
        "reason",
        "upstream_status",
    }
)
_INPUT_FIELDS = frozenset({"bytes", "filename", "sha256"})
_METRIC_FIELDS = frozenset({"denominator", "label", "unit", "value"})
_DENOMINATOR_FIELDS = frozenset({"label", "value"})
_TOP_HEALTH_FIELDS = frozenset(
    {
        "counts",
        "live_definition",
        "reporting_definition",
        "required_live",
        "required_reporting",
        "required_total",
        "status",
    }
)
_HEALTH_COUNT_FIELDS = frozenset({"corrupt", "degraded", "live", "missing", "stale"})
_LAYER_FIELDS = frozenset(
    {"id", "n_degraded", "n_live", "n_reporting", "n_total", "signal_ids", "status", "title"}
)
_ALERT_FIELDS = frozenset({"id", "kind", "severity", "source_id", "summary", "title"})
_CONFIG_FIELDS = frozenset({"schema_version", "feed", "sections", "signals"})
_CONFIG_FEED_FIELDS = frozenset({"id", "title", "scope", "url"})
_CONFIG_SECTION_FIELDS = frozenset({"dek", "id", "order", "title"})
_CONFIG_SIGNAL_FIELDS = frozenset(
    {
        "claim_template",
        "claim_type",
        "dek_template",
        "headline_template",
        "id",
        "limitations",
        "name",
        "order",
        "priority",
        "related_signal_ids",
        "section",
        "slug",
        "type",
    }
)

_SIGNAL_STATUSES = frozenset({"live", "degraded", "stale", "missing", "corrupt"})
_NONLIVE_STATUSES = _SIGNAL_STATUSES - {"live"}
_LAYER_STATUSES = frozenset({"healthy", "degraded", "unavailable"})
_STORY_TYPES = frozenset({"analysis", "measurement", "methodology", "integrity", "intelligence"})
_PRIORITIES = frozenset({"lead", "high", "standard", "background"})
_LIVE_CLAIM_TYPES = frozenset({"finding", "observation", "method", "integrity"})


class NewsroomError(ValueError):
    """The newsroom input or editorial contract is unsafe or inconsistent."""


def _fail(message: str) -> None:
    raise NewsroomError(message)


def _expect_object(value: object, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(f"{path} must be an object")
    return value


def _expect_array(value: object, path: str) -> list[Any]:
    if type(value) is not list:
        _fail(f"{path} must be an array")
    return value


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        _fail(f"{path} fields do not match contract (missing={missing}, unknown={unknown})")


def _safe_string(
    value: object,
    path: str,
    *,
    allow_empty: bool = False,
    max_length: int = 8_192,
    multiline: bool = False,
) -> str:
    if type(value) is not str:
        _fail(f"{path} must be a string")
    if len(value) > max_length:
        _fail(f"{path} is too long")
    if not allow_empty and not value.strip():
        _fail(f"{path} must not be empty")
    for char in value:
        category = unicodedata.category(char)
        if category in {"Cf", "Cs"}:
            _fail(f"{path} contains unsafe Unicode control characters")
        if category == "Cc" and char not in ("\n", "\r", "\t"):
            _fail(f"{path} contains unsafe control characters")
        if not multiline and char in ("\n", "\r", "\t"):
            _fail(f"{path} must be single-line text")
    if _UNSAFE_TEXT_RE.search(value):
        _fail(f"{path} contains unsafe active-content text")
    return value


def _identifier(value: object, path: str) -> str:
    text = _safe_string(value, path, max_length=64)
    if not _ID_RE.fullmatch(text):
        _fail(f"{path} is not a safe identifier")
    return text


def _safe_int(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        _fail(f"{path} must be an integer (not bool)")
    if value < minimum or value > _SAFE_INTEGER:
        _fail(f"{path} is outside the safe integer range")
    return value


def _finite_number(value: object, path: str) -> int | float:
    if type(value) not in (int, float):
        _fail(f"{path} must be a number (not bool)")
    if type(value) is int:
        if abs(value) > _SAFE_INTEGER:
            _fail(f"{path} is outside the safe integer range")
        return value
    if not math.isfinite(value):
        _fail(f"{path} must be finite")
    return value


def _nullable_string(value: object, path: str, *, max_length: int = 8_192) -> str | None:
    if value is None:
        return None
    return _safe_string(value, path, max_length=max_length)


def _timestamp(value: object, path: str) -> str:
    text = _safe_string(value, path, max_length=20)
    if not _TIMESTAMP_RE.fullmatch(text):
        _fail(f"{path} must be a UTC timestamp with second precision")
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise NewsroomError(f"{path} is not a real timestamp") from exc
    return text


def _timestamp_value(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _enum(value: object, allowed: frozenset[str], path: str) -> str:
    text = _safe_string(value, path, max_length=64)
    if text not in allowed:
        _fail(f"{path} must be one of {sorted(allowed)}, got {text!r}")
    return text


def _validate_json_tree(value: object, path: str = "$") -> None:
    """Reject JSON extensions, non-finite numbers, and invisible active text.

    Signal payloads are intentionally opaque to this transform, but validating the
    entire tree prevents a permissive JSON parser from smuggling NaN, duplicate-file
    surprises, or control-character content through the publication boundary.
    """

    if value is None or type(value) is bool:
        return
    if type(value) is str:
        _safe_string(value, path, allow_empty=True, max_length=65_536, multiline=True)
        return
    if type(value) in (int, float):
        _finite_number(value, path)
        return
    if type(value) is list:
        if len(value) > 100_000:
            _fail(f"{path} contains too many items")
        for index, child in enumerate(value):
            _validate_json_tree(child, f"{path}[{index}]")
        return
    if type(value) is dict:
        if len(value) > 100_000:
            _fail(f"{path} contains too many fields")
        for key, child in value.items():
            if type(key) is not str:
                _fail(f"{path} contains a non-string key")
            _safe_string(key, f"{path} key", allow_empty=False, max_length=256)
            _validate_json_tree(child, f"{path}.{key}")
        return
    _fail(f"{path} contains non-JSON value {type(value).__name__}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NewsroomError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise NewsroomError(f"non-finite JSON constant {value!r}")


def _load_json(path: Path | str, purpose: str) -> dict[str, Any]:
    source_path = Path(path)
    try:
        text = source_path.read_text(encoding="utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NewsroomError(f"cannot read {purpose}: {source_path}") from exc
    result = _expect_object(document, purpose)
    _validate_json_tree(result)
    return result


def canonical_json_bytes(document: object) -> bytes:
    """Return the v1 canonical JSON encoding used for claim fingerprints."""

    _validate_json_tree(document)
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise NewsroomError("document cannot be encoded as canonical JSON") from exc
    return encoded.encode("utf-8")


def _validate_template(value: object, path: str) -> str:
    template = _safe_string(value, path, max_length=600)
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise NewsroomError(f"{path} is not a valid template") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in _TEMPLATE_FIELDS:
            _fail(f"{path} uses unknown placeholder {field_name!r}")
        if format_spec or conversion:
            _fail(f"{path} may not use conversions or format specifications")
    return template


def load_newsroom_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load and strictly validate the curated editorial contract."""

    config = _load_json(path, "newsroom config")
    _exact_fields(config, _CONFIG_FIELDS, "newsroom config")
    if config["schema_version"] != CONFIG_SCHEMA_VERSION:
        _fail(f"unsupported newsroom config version {config['schema_version']!r}")

    feed = _expect_object(config["feed"], "newsroom config.feed")
    _exact_fields(feed, _CONFIG_FEED_FIELDS, "newsroom config.feed")
    if _identifier(feed["id"], "newsroom config.feed.id") != FEED_ID:
        _fail("newsroom config.feed.id does not match the v1 feed id")
    _safe_string(feed["title"], "newsroom config.feed.title", max_length=160)
    _safe_string(feed["scope"], "newsroom config.feed.scope", max_length=1_000)
    if feed["url"] != FEED_URL:
        _fail("newsroom config.feed.url does not match the canonical news URL")

    sections = _expect_array(config["sections"], "newsroom config.sections")
    if not sections:
        _fail("newsroom config.sections must not be empty")
    section_ids: set[str] = set()
    section_orders: set[int] = set()
    for index, raw_section in enumerate(sections):
        path_prefix = f"newsroom config.sections[{index}]"
        section = _expect_object(raw_section, path_prefix)
        _exact_fields(section, _CONFIG_SECTION_FIELDS, path_prefix)
        section_id = _identifier(section["id"], f"{path_prefix}.id")
        order = _safe_int(section["order"], f"{path_prefix}.order", minimum=1)
        if section_id in section_ids:
            _fail(f"duplicate newsroom section id {section_id!r}")
        if order in section_orders:
            _fail(f"duplicate newsroom section order {order}")
        section_ids.add(section_id)
        section_orders.add(order)
        _safe_string(section["title"], f"{path_prefix}.title", max_length=100)
        _safe_string(section["dek"], f"{path_prefix}.dek", max_length=320)

    signals = _expect_array(config["signals"], "newsroom config.signals")
    if not signals:
        _fail("newsroom config.signals must not be empty")
    signal_ids: set[str] = set()
    slugs: set[str] = set()
    positions: set[tuple[str, int]] = set()
    related_by_signal: dict[str, list[str]] = {}
    for index, raw_signal in enumerate(signals):
        path_prefix = f"newsroom config.signals[{index}]"
        signal = _expect_object(raw_signal, path_prefix)
        _exact_fields(signal, _CONFIG_SIGNAL_FIELDS, path_prefix)
        signal_id = _identifier(signal["id"], f"{path_prefix}.id")
        if signal_id in signal_ids:
            _fail(f"duplicate newsroom signal id {signal_id!r}")
        signal_ids.add(signal_id)
        slug = _safe_string(signal["slug"], f"{path_prefix}.slug", max_length=96)
        if not _SLUG_RE.fullmatch(slug):
            _fail(f"{path_prefix}.slug is not permalink-safe")
        if slug in slugs:
            _fail(f"duplicate newsroom story slug {slug!r}")
        slugs.add(slug)
        section = _identifier(signal["section"], f"{path_prefix}.section")
        if section not in section_ids:
            _fail(f"{path_prefix}.section names unknown section {section!r}")
        order = _safe_int(signal["order"], f"{path_prefix}.order", minimum=1)
        if (section, order) in positions:
            _fail(f"duplicate story order {order} in section {section!r}")
        positions.add((section, order))
        _safe_string(signal["name"], f"{path_prefix}.name", max_length=100)
        _enum(signal["type"], _STORY_TYPES, f"{path_prefix}.type")
        _enum(signal["priority"], _PRIORITIES, f"{path_prefix}.priority")
        _enum(signal["claim_type"], _LIVE_CLAIM_TYPES, f"{path_prefix}.claim_type")
        _validate_template(signal["headline_template"], f"{path_prefix}.headline_template")
        _validate_template(signal["dek_template"], f"{path_prefix}.dek_template")
        _validate_template(signal["claim_template"], f"{path_prefix}.claim_template")
        limitations = _expect_array(signal["limitations"], f"{path_prefix}.limitations")
        if not limitations or len(limitations) > 8:
            _fail(f"{path_prefix}.limitations must contain 1 to 8 explicit limitations")
        if len(set(limitations)) != len(limitations):
            _fail(f"{path_prefix}.limitations contains duplicates")
        for limit_index, limitation in enumerate(limitations):
            _safe_string(
                limitation,
                f"{path_prefix}.limitations[{limit_index}]",
                max_length=500,
            )
        related = _expect_array(signal["related_signal_ids"], f"{path_prefix}.related_signal_ids")
        if len(related) > 12:
            _fail(f"{path_prefix}.related_signal_ids contains too many entries")
        normalized_related = [
            _identifier(item, f"{path_prefix}.related_signal_ids[{related_index}]")
            for related_index, item in enumerate(related)
        ]
        if len(normalized_related) != len(set(normalized_related)):
            _fail(f"{path_prefix}.related_signal_ids contains duplicates")
        if signal_id in normalized_related:
            _fail(f"{path_prefix}.related_signal_ids may not contain itself")
        related_by_signal[signal_id] = normalized_related

    for signal_id, related_ids in related_by_signal.items():
        unknown = sorted(set(related_ids) - signal_ids)
        if unknown:
            _fail(f"newsroom signal {signal_id!r} relates to unknown signals {unknown}")
    return config


def _validate_url(value: object, filename: str, path: str) -> str:
    url = _safe_string(value, path, max_length=300)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "palimpsest.info"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path != f"/readings/{filename}"
    ):
        _fail(f"{path} is not the canonical HTTPS evidence URL for {filename!r}")
    return url


def _validate_metric(value: object, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    metric = _expect_object(value, path)
    _exact_fields(metric, _METRIC_FIELDS, path)
    label = _safe_string(metric["label"], f"{path}.label", max_length=100)
    unit = _safe_string(metric["unit"], f"{path}.unit", max_length=64)
    number = _finite_number(metric["value"], f"{path}.value")
    raw_denominator = metric["denominator"]
    denominator: dict[str, Any] | None = None
    if raw_denominator is not None:
        denominator = _expect_object(raw_denominator, f"{path}.denominator")
        _exact_fields(denominator, _DENOMINATOR_FIELDS, f"{path}.denominator")
        denominator = {
            "label": _safe_string(
                denominator["label"], f"{path}.denominator.label", max_length=100
            ),
            "value": _safe_int(
                denominator["value"], f"{path}.denominator.value", minimum=0
            ),
        }
    return {"label": label, "value": number, "unit": unit, "denominator": denominator}


def _validate_signal(raw_signal: object, path: str, feed_generated_at: str) -> dict[str, Any]:
    signal = _expect_object(raw_signal, path)
    _exact_fields(signal, _SIGNAL_FIELDS, path)
    signal_id = _identifier(signal["id"], f"{path}.id")
    layer = _identifier(signal["layer"], f"{path}.layer")
    status = _enum(signal["status"], _SIGNAL_STATUSES, f"{path}.status")
    if type(signal["live"]) is not bool:
        _fail(f"{path}.live must be a boolean")
    if type(signal["optional"]) is not bool:
        _fail(f"{path}.optional must be a boolean")
    if type(signal["payload_complete"]) is not bool:
        _fail(f"{path}.payload_complete must be a boolean")
    if signal["payload"] is None and signal["payload_complete"] is not False:
        _fail(f"{path} missing payloads must set payload_complete false")

    health = _expect_object(signal["health"], f"{path}.health")
    _exact_fields(health, _SIGNAL_HEALTH_FIELDS, f"{path}.health")
    if type(health["ok"]) is not bool:
        _fail(f"{path}.health.ok must be a boolean")
    expected_live = status == "live"
    if signal["live"] is not expected_live or health["ok"] is not expected_live:
        _fail(f"{path} has inconsistent live/status/health.ok semantics")
    _safe_string(health["reason"], f"{path}.health.reason", max_length=600)
    age_hours = health["age_hours"]
    if age_hours is not None and _finite_number(age_hours, f"{path}.health.age_hours") < 0:
        _fail(f"{path}.health.age_hours must be non-negative")
    _nullable_string(health["collector_reason"], f"{path}.health.collector_reason", max_length=600)
    _nullable_string(health["collector_status"], f"{path}.health.collector_status", max_length=100)
    _nullable_string(health["upstream_status"], f"{path}.health.upstream_status", max_length=100)
    pipeline_checked_at = health["pipeline_checked_at"]
    if pipeline_checked_at is not None:
        # This timestamp remains inside the source payload boundary and may carry fractions.
        text = _safe_string(pipeline_checked_at, f"{path}.health.pipeline_checked_at", max_length=40)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NewsroomError(f"{path}.health.pipeline_checked_at is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _fail(f"{path}.health.pipeline_checked_at must be timezone-aware")

    source_timestamp = signal["source_timestamp"]
    freshness_deadline = signal["freshness_deadline"]
    if source_timestamp is not None:
        source_timestamp = _timestamp(source_timestamp, f"{path}.source_timestamp")
        if _timestamp_value(source_timestamp) > _timestamp_value(feed_generated_at):
            _fail(f"{path}.source_timestamp is future-dated")
    if freshness_deadline is not None:
        freshness_deadline = _timestamp(freshness_deadline, f"{path}.freshness_deadline")

    input_record = _expect_object(signal["input"], f"{path}.input")
    _exact_fields(input_record, _INPUT_FIELDS, f"{path}.input")
    filename = _safe_string(input_record["filename"], f"{path}.input.filename", max_length=128)
    if not _FILENAME_RE.fullmatch(filename) or "/" in filename or ".." in filename:
        _fail(f"{path}.input.filename is not a safe JSON basename")
    sha256 = input_record["sha256"]
    if sha256 is not None:
        sha256 = _safe_string(sha256, f"{path}.input.sha256", max_length=64)
        if not _SHA256_RE.fullmatch(sha256):
            _fail(f"{path}.input.sha256 is not a lowercase SHA-256 digest")
    input_bytes = input_record["bytes"]
    if input_bytes is not None:
        input_bytes = _safe_int(input_bytes, f"{path}.input.bytes", minimum=0)

    if status == "missing":
        if any(value is not None for value in (source_timestamp, freshness_deadline, sha256, input_bytes)):
            _fail(f"{path} missing signals may not claim source evidence")
        if signal["payload"] is not None or signal["metric"] is not None:
            _fail(f"{path} missing signals may not carry a payload or metric")
    else:
        if source_timestamp is None or freshness_deadline is None or sha256 is None or input_bytes is None:
            _fail(f"{path} reporting signals require timestamped, hashed input evidence")
    if status == "live":
        if _timestamp_value(source_timestamp) > _timestamp_value(freshness_deadline):
            _fail(f"{path} is labelled live after its freshness deadline")
        if signal["payload"] is None:
            _fail(f"{path} live signals require a payload")
    if status == "stale" and _timestamp_value(feed_generated_at) <= _timestamp_value(freshness_deadline):
        _fail(f"{path} is labelled stale before its freshness deadline")

    cadence_hours = _finite_number(signal["cadence_hours"], f"{path}.cadence_hours")
    if cadence_hours <= 0:
        _fail(f"{path}.cadence_hours must be positive")
    _safe_string(signal["title"], f"{path}.title", max_length=160)
    _safe_string(signal["summary"], f"{path}.summary", max_length=2_000)
    _safe_string(signal["scope"], f"{path}.scope", max_length=2_000)
    _safe_string(signal["source"], f"{path}.source", max_length=2_000)
    _safe_string(signal["method"], f"{path}.method", max_length=8_000)
    method_version = signal["method_version"]
    if method_version is not None:
        if type(method_version) is int:
            _safe_int(method_version, f"{path}.method_version", minimum=1)
        elif type(method_version) is str:
            _safe_string(method_version, f"{path}.method_version", max_length=100)
        else:
            _fail(f"{path}.method_version must be a string, positive integer, or null")
    metric = _validate_metric(signal["metric"], f"{path}.metric")
    evidence_url = _validate_url(signal["raw_url"], filename, f"{path}.raw_url")

    analysis_warmup = False
    analysis_history = None
    analysis_history_required = None
    if signal_id == "believability" and status == "live":
        payload = _expect_object(signal["payload"], f"{path}.payload")
        if payload.get("label") == "warming_up":
            analysis_history = _safe_int(
                payload.get("n_history"), f"{path}.payload.n_history", minimum=0
            )
            analysis_history_required = _safe_int(
                payload.get("n_history_required", _BELIEVABILITY_MIN_HISTORY),
                f"{path}.payload.n_history_required",
                minimum=1,
            )
            if analysis_history >= analysis_history_required:
                _fail(f"{path} warm-up history has already reached its declared gate")
            analysis_warmup = True

    return {
        "id": signal_id,
        "layer": layer,
        "status": status,
        "source_timestamp": source_timestamp,
        "freshness_deadline": freshness_deadline,
        "health_reason": health["reason"],
        "input_filename": filename,
        "input_sha256": sha256,
        "input_bytes": input_bytes,
        "evidence_url": evidence_url,
        "method": signal["method"],
        "method_version": method_version,
        "metric": metric,
        "analysis_warmup": analysis_warmup,
        "analysis_history": analysis_history,
        "analysis_history_required": analysis_history_required,
    }


def _validate_source(document: Mapping[str, object]) -> list[dict[str, Any]]:
    _exact_fields(document, _TOP_LEVEL_FIELDS, "osint source")
    if document["schema_version"] != SOURCE_SCHEMA_VERSION:
        _fail(f"unsupported osint source version {document['schema_version']!r}")
    if document["method_version"] != 1 or type(document["method_version"]) is not int:
        _fail("osint source method_version must be integer 1")
    generated_at = _timestamp(document["generated_at"], "osint source.generated_at")
    commit = _safe_string(document["input_commit"], "osint source.input_commit", max_length=40)
    if not _COMMIT_RE.fullmatch(commit):
        _fail("osint source.input_commit must be a lowercase 40-character commit id")
    _safe_string(document["headline"], "osint source.headline", max_length=1_000)
    _safe_string(document["method"], "osint source.method", max_length=4_000)
    _safe_string(document["scope"], "osint source.scope", max_length=2_000)
    _safe_string(document["source"], "osint source.source", max_length=2_000)

    raw_signals = _expect_array(document["signals"], "osint source.signals")
    n_total = _safe_int(document["n_signals_total"], "osint source.n_signals_total")
    n_live = _safe_int(document["n_signals_live"], "osint source.n_signals_live")
    n_reporting = _safe_int(document["n_signals_reporting"], "osint source.n_signals_reporting")
    if n_total != len(raw_signals):
        _fail("osint source.n_signals_total does not match signals")
    signals = [
        _validate_signal(raw_signal, f"osint source.signals[{index}]", generated_at)
        for index, raw_signal in enumerate(raw_signals)
    ]
    ids = [signal["id"] for signal in signals]
    if len(ids) != len(set(ids)):
        _fail("osint source contains duplicate signal ids")
    actual_live = sum(signal["status"] == "live" for signal in signals)
    actual_reporting = sum(signal["status"] not in {"missing", "corrupt"} for signal in signals)
    if n_live != actual_live or n_reporting != actual_reporting:
        _fail("osint source live/reporting counts do not match signal statuses")

    top_health = _expect_object(document["health"], "osint source.health")
    _exact_fields(top_health, _TOP_HEALTH_FIELDS, "osint source.health")
    counts = _expect_object(top_health["counts"], "osint source.health.counts")
    _exact_fields(counts, _HEALTH_COUNT_FIELDS, "osint source.health.counts")
    for key in _HEALTH_COUNT_FIELDS:
        _safe_int(counts[key], f"osint source.health.counts.{key}")
        expected = sum(signal["status"] == key for signal in signals)
        if counts[key] != expected:
            _fail(f"osint source.health.counts.{key} does not match signal statuses")
    if sum(counts.values()) != n_total:
        _fail("osint source.health.counts does not sum to n_signals_total")
    _safe_string(top_health["live_definition"], "osint source.health.live_definition", max_length=1_000)
    _safe_string(
        top_health["reporting_definition"],
        "osint source.health.reporting_definition",
        max_length=1_000,
    )
    for field in ("required_live", "required_reporting", "required_total"):
        _safe_int(top_health[field], f"osint source.health.{field}")
    _enum(top_health["status"], frozenset({"healthy", "degraded"}), "osint source.health.status")

    layers = _expect_array(document["layers"], "osint source.layers")
    layer_ids: set[str] = set()
    layer_signal_ids: set[str] = set()
    for index, raw_layer in enumerate(layers):
        path = f"osint source.layers[{index}]"
        layer = _expect_object(raw_layer, path)
        _exact_fields(layer, _LAYER_FIELDS, path)
        layer_id = _identifier(layer["id"], f"{path}.id")
        if layer_id in layer_ids:
            _fail(f"duplicate osint layer id {layer_id!r}")
        layer_ids.add(layer_id)
        _safe_string(layer["title"], f"{path}.title", max_length=120)
        _enum(layer["status"], _LAYER_STATUSES, f"{path}.status")
        for field in ("n_degraded", "n_live", "n_reporting", "n_total"):
            _safe_int(layer[field], f"{path}.{field}")
        member_ids = _expect_array(layer["signal_ids"], f"{path}.signal_ids")
        normalized = [
            _identifier(member, f"{path}.signal_ids[{member_index}]")
            for member_index, member in enumerate(member_ids)
        ]
        if len(normalized) != len(set(normalized)):
            _fail(f"{path}.signal_ids contains duplicates")
        if layer["n_total"] != len(normalized):
            _fail(f"{path}.n_total does not match signal_ids")
        overlap = layer_signal_ids & set(normalized)
        if overlap:
            _fail(f"signals occur in more than one osint layer: {sorted(overlap)}")
        layer_signal_ids.update(normalized)
    if layer_signal_ids != set(ids):
        _fail("osint source layer membership does not match signals")
    if any(signal["layer"] not in layer_ids for signal in signals):
        _fail("osint source signal names an unknown layer")
    for signal in signals:
        layer = next(item for item in layers if item["id"] == signal["layer"])
        if signal["id"] not in layer["signal_ids"]:
            _fail(f"osint source signal {signal['id']!r} disagrees with layer membership")

    alerts = _expect_array(document["alerts"], "osint source.alerts")
    alert_ids: set[str] = set()
    for index, raw_alert in enumerate(alerts):
        path = f"osint source.alerts[{index}]"
        alert = _expect_object(raw_alert, path)
        _exact_fields(alert, _ALERT_FIELDS, path)
        alert_id = _identifier(alert["id"], f"{path}.id")
        if alert_id in alert_ids:
            _fail(f"duplicate osint alert id {alert_id!r}")
        alert_ids.add(alert_id)
        source_id = _identifier(alert["source_id"], f"{path}.source_id")
        if source_id not in set(ids):
            _fail(f"{path}.source_id names unknown signal {source_id!r}")
        for field in ("kind", "severity", "summary", "title"):
            _safe_string(alert[field], f"{path}.{field}", max_length=1_000)
    return signals


def _format_number(value: int | float | None) -> str:
    if value is None:
        return "not reported"
    if type(value) is int:
        return f"{value:,}"
    return format(value, ".12g")


def _board_story_headline(board_headline: str) -> str:
    """Condense only the board builder's declared headline variants.

    The feed-level headline remains the complete normalized roll-up.  A story H1 is
    shorter, but is never made by truncating or echoing arbitrary upstream text.
    """

    single = re.match(
        r"^Upstream board reports: single layer elevated: ([a-z][a-z0-9_-]{0,63})\.",
        board_headline,
    )
    if single:
        layer = single.group(1).replace("_", " ").replace("-", " ").capitalize()
        return f"{layer} layer elevated in the latest board synthesis"
    if re.match(
        r"^Upstream board reports: MULTI-LAYER CO-MOVEMENT: "
        r"[a-z][a-z0-9_-]*(?: \+ [a-z][a-z0-9_-]*)+ elevated together\.",
        board_headline,
    ):
        return "Multiple layers elevated together in the latest board synthesis"
    if re.match(
        r"^Upstream board reports: elevated signals: "
        r"[a-z][a-z0-9_-]*(?:, [a-z][a-z0-9_-]*)*\.",
        board_headline,
    ):
        return "Signal-level elevations detected in the latest board synthesis"
    if board_headline.startswith(
        "Upstream board reports: no signal exceeds its own history beyond what chance explains."
    ):
        return "No signal clears the board's historical-elevation threshold"
    if board_headline.startswith("No current board-level analytic headline is available."):
        return "No current board-level analytic headline is available"
    _fail("osint source.headline is not a declared board-synthesis variant")


def _template_values(
    signal: Mapping[str, Any],
    config: Mapping[str, Any],
    board_headline: str,
    board_story_headline: str,
) -> dict[str, str]:
    metric = signal["metric"] if signal["status"] == "live" else None
    denominator = metric["denominator"] if metric is not None else None
    value = metric["value"] if metric is not None else None
    percent: int | float | None = value
    if metric is not None and metric["unit"] == "ratio":
        percent = value * 100
    return {
        "board_headline": board_headline,
        "board_story_headline": board_story_headline,
        "signal_name": config["name"],
        "status": signal["status"],
        "metric_label": metric["label"] if metric is not None else "current metric",
        "value": _format_number(value),
        "value_percent": _format_number(percent),
        "unit": metric["unit"] if metric is not None else "",
        "denominator": _format_number(denominator["value"] if denominator else None),
        "denominator_label": denominator["label"] if denominator else "not reported",
    }


def _render(template: str, values: Mapping[str, str], path: str) -> str:
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError) as exc:  # Defensive after config validation.
        raise NewsroomError(f"cannot render {path}") from exc
    return _safe_string(rendered, path, max_length=1_000)


def _news_metric(signal: Mapping[str, Any]) -> dict[str, Any]:
    metric = signal["metric"] if signal["status"] == "live" else None
    denominator = metric["denominator"] if metric is not None else None
    return {
        "label": metric["label"] if metric is not None else None,
        "value": metric["value"] if metric is not None else None,
        "unit": metric["unit"] if metric is not None else None,
        "denominator": {
            "label": denominator["label"] if denominator is not None else None,
            "value": denominator["value"] if denominator is not None else None,
        },
    }


def _story(
    signal: Mapping[str, Any],
    editorial: Mapping[str, Any],
    generated_at: str,
    board_headline: str,
    board_story_headline: str,
) -> dict[str, Any]:
    status = signal["status"]
    values = _template_values(signal, editorial, board_headline, board_story_headline)
    if status == "live" and signal["analysis_warmup"]:
        history = signal["analysis_history"]
        required = signal["analysis_history_required"]
        headline = "The monthly state-data comparison is live and building its baseline"
        dek = (
            "All three physical-activity components are present, but the historical "
            "uncertainty band is not mature enough for a divergence finding."
        )
        claim_type = "observation"
        statement = (
            "The current believability collection is complete; divergence remains "
            f"withheld while its baseline has {history} of {required} required prior months."
        )
        metric = _news_metric(signal)
        limitations = [
            f"No drift finding is claimed until {required} prior monthly gaps exist.",
            *editorial["limitations"],
        ]
    elif status == "live":
        headline = _render(editorial["headline_template"], values, f"{signal['id']} headline")
        dek = _render(editorial["dek_template"], values, f"{signal['id']} dek")
        claim_type = editorial["claim_type"]
        statement = _render(editorial["claim_template"], values, f"{signal['id']} claim")
        metric = _news_metric(signal)
        limitations = list(editorial["limitations"])
    else:
        headline = f"{editorial['name']}: no current finding"
        dek = (
            f"The source is {status}. Palimpsest keeps the availability and evidence "
            "record visible without publishing retained values as current news."
        )
        claim_type = "availability"
        statement = (
            f"No current finding is published for {editorial['name']} because the "
            f"source status is {status}."
        )
        metric = _news_metric(signal)
        limitations = [
            f"Current finding withheld: {signal['health_reason']}",
            *editorial["limitations"],
        ]
    if len(headline) > 160:
        _fail(f"story {signal['id']!r} headline exceeds 160 characters")
    if len(limitations) != len(set(limitations)):
        _fail(f"story {signal['id']!r} contains duplicate limitations")

    claim_core = {
        "claim_type": claim_type,
        "metric": metric,
        "signal_id": signal["id"],
        "statement": statement,
        "status": status,
    }
    fingerprint = "sha256:" + hashlib.sha256(canonical_json_bytes(claim_core)).hexdigest()
    timestamp = signal["source_timestamp"] or generated_at
    slug = editorial["slug"]
    return {
        "id": f"palimpsest-news:{signal['id']}",
        "slug": slug,
        "url": f"{FEED_URL}{slug}/",
        "signal_id": signal["id"],
        "headline": headline,
        "dek": dek,
        "section": editorial["section"],
        "order": editorial["order"],
        "type": editorial["type"],
        "priority": editorial["priority"],
        "status": status,
        "published_at": timestamp,
        "modified_at": timestamp,
        "claim_fingerprint": fingerprint,
        "metric": metric,
        "claims": [{"type": claim_type, "statement": statement}],
        "evidence": {
            "url": signal["evidence_url"],
            "input": {
                "filename": signal["input_filename"],
                "sha256": signal["input_sha256"],
                "bytes": signal["input_bytes"],
            },
            "source_timestamp": signal["source_timestamp"],
        },
        "method": {
            "summary": signal["method"],
            "version": signal["method_version"],
        },
        "limitations": limitations,
        "related_signal_ids": list(editorial["related_signal_ids"]),
    }


def transform_osint_feed(
    source: Mapping[str, object], config: Mapping[str, object]
) -> dict[str, Any]:
    """Transform validated ``osint-china.v1`` data into ``palimpsest-news.v1``.

    The function has no clock or filesystem reads, so the same two mappings always
    produce byte-identical output through :func:`canonical_json_bytes`.
    """

    if type(source) is not dict or type(config) is not dict:
        _fail("source and config must be plain JSON objects")
    _validate_json_tree(source)
    _validate_json_tree(config)
    # Validate mappings directly as callers may bypass the strict file loader.
    validated_config = _validate_config_mapping(config)
    signals = _validate_source(source)
    config_signals = {item["id"]: item for item in validated_config["signals"]}
    source_ids = {item["id"] for item in signals}
    if source_ids != set(config_signals):
        missing = sorted(source_ids - set(config_signals))
        unknown = sorted(set(config_signals) - source_ids)
        _fail(f"source/config signal ids differ (missing_config={missing}, unknown_config={unknown})")

    section_order = {item["id"]: item["order"] for item in validated_config["sections"]}
    for signal in signals:
        editorial = config_signals[signal["id"]]
        if editorial["section"] != signal["layer"]:
            _fail(
                f"newsroom signal {signal['id']!r} section {editorial['section']!r} "
                f"does not match source layer {signal['layer']!r}"
            )
    ordered_signals = sorted(
        signals,
        key=lambda item: (
            section_order[config_signals[item["id"]]["section"]],
            config_signals[item["id"]]["order"],
            item["id"],
        ),
    )
    generated_at = source["generated_at"]
    board_story_headline = _board_story_headline(source["headline"])
    stories = [
        _story(
            signal,
            config_signals[signal["id"]],
            generated_at,
            source["headline"],
            board_story_headline,
        )
        for signal in ordered_signals
    ]
    sections = [
        {"id": item["id"], "title": item["title"], "dek": item["dek"], "order": item["order"]}
        for item in sorted(validated_config["sections"], key=lambda item: (item["order"], item["id"]))
    ]
    feed = {
        "schema_version": NEWS_SCHEMA_VERSION,
        "feed_id": validated_config["feed"]["id"],
        "title": validated_config["feed"]["title"],
        "headline": source["headline"],
        "url": validated_config["feed"]["url"],
        "generated_at": generated_at,
        "n_stories": len(stories),
        "source": SOURCE_URL,
        "source_commit": source["input_commit"],
        "method": NEWS_METHOD,
        "scope": validated_config["feed"]["scope"],
        "coverage": {
            "total": source["n_signals_total"],
            "reporting": source["n_signals_reporting"],
            "live": source["n_signals_live"],
            "status": source["health"]["status"],
            "counts": {
                key: source["health"]["counts"][key]
                for key in ("live", "degraded", "stale", "missing", "corrupt")
            },
        },
        "sections": sections,
        "stories": stories,
    }
    # A final serialization pass proves that no accidental non-JSON/non-finite value
    # can escape through future implementation changes.
    canonical_json_bytes(feed)
    return feed


def _validate_config_mapping(config: Mapping[str, object]) -> dict[str, Any]:
    """Validate an in-memory config using the same strict rules as the file API."""

    # Keep one validator rather than a subtly weaker code path: canonical bytes are
    # decoded with duplicate-safe data already represented as a mapping.
    temp = json.loads(canonical_json_bytes(config).decode("utf-8"))
    # The body mirrors ``load_newsroom_config`` without another filesystem read.
    _exact_fields(temp, _CONFIG_FIELDS, "newsroom config")
    if temp["schema_version"] != CONFIG_SCHEMA_VERSION:
        _fail(f"unsupported newsroom config version {temp['schema_version']!r}")
    feed = _expect_object(temp["feed"], "newsroom config.feed")
    _exact_fields(feed, _CONFIG_FEED_FIELDS, "newsroom config.feed")
    if _identifier(feed["id"], "newsroom config.feed.id") != FEED_ID:
        _fail("newsroom config.feed.id does not match the v1 feed id")
    _safe_string(feed["title"], "newsroom config.feed.title", max_length=160)
    _safe_string(feed["scope"], "newsroom config.feed.scope", max_length=1_000)
    if feed["url"] != FEED_URL:
        _fail("newsroom config.feed.url does not match the canonical news URL")

    sections = _expect_array(temp["sections"], "newsroom config.sections")
    section_ids: set[str] = set()
    section_orders: set[int] = set()
    if not sections:
        _fail("newsroom config.sections must not be empty")
    for index, section_value in enumerate(sections):
        path = f"newsroom config.sections[{index}]"
        section = _expect_object(section_value, path)
        _exact_fields(section, _CONFIG_SECTION_FIELDS, path)
        section_id = _identifier(section["id"], f"{path}.id")
        order = _safe_int(section["order"], f"{path}.order", minimum=1)
        if section_id in section_ids or order in section_orders:
            _fail("newsroom config has duplicate section id or order")
        section_ids.add(section_id)
        section_orders.add(order)
        _safe_string(section["title"], f"{path}.title", max_length=100)
        _safe_string(section["dek"], f"{path}.dek", max_length=320)

    signals = _expect_array(temp["signals"], "newsroom config.signals")
    if not signals:
        _fail("newsroom config.signals must not be empty")
    ids: set[str] = set()
    slugs: set[str] = set()
    positions: set[tuple[str, int]] = set()
    relations: dict[str, list[str]] = {}
    for index, signal_value in enumerate(signals):
        path = f"newsroom config.signals[{index}]"
        signal = _expect_object(signal_value, path)
        _exact_fields(signal, _CONFIG_SIGNAL_FIELDS, path)
        signal_id = _identifier(signal["id"], f"{path}.id")
        if signal_id in ids:
            _fail(f"duplicate newsroom signal id {signal_id!r}")
        ids.add(signal_id)
        slug = _safe_string(signal["slug"], f"{path}.slug", max_length=96)
        if not _SLUG_RE.fullmatch(slug) or slug in slugs:
            _fail(f"{path}.slug is duplicate or not permalink-safe")
        slugs.add(slug)
        section = _identifier(signal["section"], f"{path}.section")
        if section not in section_ids:
            _fail(f"{path}.section names unknown section {section!r}")
        order = _safe_int(signal["order"], f"{path}.order", minimum=1)
        if (section, order) in positions:
            _fail(f"duplicate story order {order} in section {section!r}")
        positions.add((section, order))
        _safe_string(signal["name"], f"{path}.name", max_length=100)
        _enum(signal["type"], _STORY_TYPES, f"{path}.type")
        _enum(signal["priority"], _PRIORITIES, f"{path}.priority")
        _enum(signal["claim_type"], _LIVE_CLAIM_TYPES, f"{path}.claim_type")
        for field in ("headline_template", "dek_template", "claim_template"):
            _validate_template(signal[field], f"{path}.{field}")
        limitations = _expect_array(signal["limitations"], f"{path}.limitations")
        if not 1 <= len(limitations) <= 8 or len(limitations) != len(set(limitations)):
            _fail(f"{path}.limitations must contain 1 to 8 unique values")
        for item_index, item in enumerate(limitations):
            _safe_string(item, f"{path}.limitations[{item_index}]", max_length=500)
        related = _expect_array(signal["related_signal_ids"], f"{path}.related_signal_ids")
        normalized = [
            _identifier(item, f"{path}.related_signal_ids[{item_index}]")
            for item_index, item in enumerate(related)
        ]
        if len(normalized) > 12 or len(normalized) != len(set(normalized)) or signal_id in normalized:
            _fail(f"{path}.related_signal_ids is invalid")
        relations[signal_id] = normalized
    for signal_id, related_ids in relations.items():
        unknown = sorted(set(related_ids) - ids)
        if unknown:
            _fail(f"newsroom signal {signal_id!r} relates to unknown signals {unknown}")
    return temp


def build_news_feed(
    source_path: Path | str = DEFAULT_SOURCE_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Load the checked-in inputs and return the deterministic newsroom feed.

    ``DEFAULT_OUTPUT_PATH`` documents the publication destination, but this function
    performs no write.  Publication remains the responsibility of the caller so a
    validation failure cannot replace the last known-good artifact.
    """

    source = _load_json(source_path, "osint source")
    config = load_newsroom_config(config_path)
    return transform_osint_feed(source, config)


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_SOURCE_PATH",
    "NEWS_SCHEMA_VERSION",
    "NewsroomError",
    "SCHEMA_PATH",
    "build_news_feed",
    "canonical_json_bytes",
    "load_newsroom_config",
    "transform_osint_feed",
]
