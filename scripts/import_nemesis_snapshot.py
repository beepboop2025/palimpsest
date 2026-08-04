"""Authenticate and import the optional private-runtime public snapshot.

This module is the publication boundary between an independently operated runtime and
the public Palimpsest repository. It authenticates the exact downloaded bytes before
strict UTF-8 and JSON parsing, then reconstructs a closed public document at every
nested level. No raw runtime object is ever written into the Pages tree.

With no ``NEMESIS_SNAPSHOT_URL`` the command is deliberately a no-op. Once a URL is
configured, a missing key, unavailable endpoint, invalid signature, schema mismatch,
or stale health contradiction fails the refresh loudly while preserving the previous
committed reading.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from core.safe_fetch import FetchError, safe_fetch_bytes


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "readings" / "nemesis-latest.json"
URL_ENV = "NEMESIS_SNAPSHOT_URL"
HMAC_KEY_ENV = "NEMESIS_SNAPSHOT_HMAC_KEY"
MAX_BYTES = 4 * 1024 * 1024
MAX_SIGNATURE_BYTES = len(b"hmac-sha256=" + b"0" * 64 + b"\n")
TIMEOUT_SECONDS = 20.0
MAX_FUTURE_SKEW_SECONDS = 300.0
PAIR_ATTEMPTS = 3
EARLIEST_TIMESTAMP = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
MAX_SAFE_INTEGER = 9_007_199_254_740_991

SCHEMA = "palimpsest-nemesis.public-snapshot"
SCHEMA_VERSION = "1.0.0"
SOURCE = "Palimpsest-Nemesis"
STATUSES = frozenset({"ok", "degraded", "starting"})
TOP_LEVEL_FIELDS = frozenset({
    "schema", "schema_version", "source", "method", "method_version", "scope",
    "status", "methods", "generated_at", "data_timestamp", "timestamps", "health",
    "coverage", "counts", "ddti", "economic", "leads",
})
METHOD_FIELDS = frozenset({"ddti", "economic", "leads"})
TIMESTAMP_FIELDS = frozenset({
    "ddti_generated_at", "economic_generated_at", "latest_fetched_at",
    "latest_published_at", "last_successful_run_at", "data_updated_at",
})
HEALTH_FIELDS = frozenset({
    "status", "live", "ready", "stale", "posts", "reasons", "freshness",
    "last_run", "last_completed_run", "last_successful_run_at",
})
FRESHNESS_FIELDS = frozenset({
    "status", "core_data_at", "age_seconds", "stale_after_seconds",
})
RUN_FIELDS = frozenset({
    "id", "started_at", "finished_at", "status", "healthy", "duration_s",
    "collection_errors",
})
COVERAGE_FIELDS = frozenset({
    "completeness", "scope", "observed_source_count", "observed_sources",
    "first_published_at", "latest_published_at", "latest_fetched_at",
    "last_successful_cycle",
})
OBSERVED_SOURCE_FIELDS = frozenset({
    "name", "posts", "first_published_at", "latest_published_at", "latest_fetched_at",
})
CYCLE_FIELDS = frozenset({
    "new", "dupes", "errors", "attempted", "succeeded", "failed", "posts_scored",
    "sources",
})
CYCLE_SOURCE_FIELDS = frozenset({
    "name", "status", "new", "dupes", "attempted", "succeeded", "failed",
})
COUNT_FIELDS = frozenset({"posts", "sources", "topics", "economic_articles", "leads"})
DDTI_FIELDS = frozenset({
    "generated_at", "n_posts", "n_posts_scored", "n_terms", "by_domain", "ranked",
})
DDTI_ROW_FIELDS = frozenset({
    "term", "domain", "attention", "novelty", "threat", "is_new", "total", "recent",
    "tag_observations", "text_observations", "direct_signal_observations", "samples",
})
ECONOMIC_FIELDS = frozenset({
    "generated_at", "metric_name", "scope", "pct", "n_econ_articles", "ranked",
})
ECONOMIC_ROW_FIELDS = frozenset({"term", "weight", "samples"})
LEAD_FIELDS = frozenset({
    "term", "domain", "score", "threat", "novelty", "n", "is_new", "samples",
})
SAMPLE_REQUIRED_FIELDS = frozenset({"title", "url"})
SAMPLE_FIELDS = frozenset({"title", "url", "source", "matched_in", "deletion_signal"})
EXPECTED_METHOD = "Observed public-source topic attention, novelty, and economic-topic share"
EXPECTED_METHOD_VERSION = "nemesis-public-v2"
EXPECTED_METHODS = {
    "ddti": "attention-novelty-signal-weighted-v1",
    "economic": "observed-economic-topic-share-v2",
    "leads": "ddti-investigative-leads-v1",
}
METHOD_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SIGNATURE = re.compile(rb"hmac-sha256=([0-9a-f]{64})\n\Z")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|authorization|password|passwd|secret|token)"
    r"\s*[:=]\s*(?!\[redacted\])[^\s,;]+"
)
_TOKEN_LITERAL = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"(?:rob_|sk-(?:proj-)?)[A-Za-z0-9_-]{16,})\b"
)
_INTERNAL_PATH = re.compile(
    r"(?<![\w:])(?:/(?:etc|home|opt|private|run|tmp|Users|var)/[^\s\"'<>]*"
    r"|[A-Za-z]:\\[^\s\"'<>]*)"
)

Fetcher = Callable[..., bytes]


class SnapshotImportError(ValueError):
    """The configured snapshot could not safely cross the publication boundary."""


def validate_https_url(url: str) -> str:
    """Accept one absolute, credential-free HTTPS URL and no redirect semantics."""
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise SnapshotImportError("snapshot URL must be a non-empty HTTPS URL")
    if any(ord(char) < 0x20 or char.isspace() for char in url):
        raise SnapshotImportError("snapshot URL contains whitespace or control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SnapshotImportError("snapshot URL is malformed") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SnapshotImportError("snapshot URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise SnapshotImportError("snapshot URL must not contain credentials")
    if parsed.query:
        raise SnapshotImportError("snapshot URL must not contain a query")
    if parsed.fragment:
        raise SnapshotImportError("snapshot URL must not contain a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise SnapshotImportError("snapshot URL has an invalid port")
    return url


def signature_url(url: str) -> str:
    """Derive the exact static sidecar path from a query-free endpoint."""
    parsed = urlsplit(validate_https_url(url))
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path + ".hmac-sha256",
        "",
        "",
    ))


def _hmac_key(value: str | bytes) -> bytes:
    if isinstance(value, str):
        try:
            key = value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise SnapshotImportError("snapshot HMAC key is not valid UTF-8") from exc
    elif isinstance(value, bytes):
        key = value
    else:
        raise SnapshotImportError("snapshot HMAC key is required")
    if len(key) < 32:
        raise SnapshotImportError("snapshot HMAC key must contain at least 32 bytes")
    return key


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotImportError(f"snapshot repeats JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SnapshotImportError(f"snapshot contains non-finite number {value}")


def _parse_document(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise SnapshotImportError("snapshot fetch must return raw bytes")
    if len(payload) > MAX_BYTES:
        raise SnapshotImportError(f"snapshot exceeds {MAX_BYTES} bytes")
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SnapshotImportError("snapshot is not valid UTF-8") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except SnapshotImportError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SnapshotImportError("snapshot is not valid bounded JSON") from exc
    if not isinstance(document, dict):
        raise SnapshotImportError("snapshot root must be an object")
    return document


def _bounded_shape(value: Any, *, depth: int = 0) -> None:
    if depth > 20:
        raise SnapshotImportError("snapshot nesting exceeds 20 levels")
    if isinstance(value, dict):
        if len(value) > 500:
            raise SnapshotImportError("snapshot object has too many fields")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 200:
                raise SnapshotImportError("snapshot contains an invalid object key")
            _bounded_shape(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 10_000:
            raise SnapshotImportError("snapshot array has too many entries")
        for child in value:
            _bounded_shape(child, depth=depth + 1)
    elif isinstance(value, str) and len(value) > 20_000:
        raise SnapshotImportError("snapshot contains an oversized string")


def _closed_object(
    value: Any,
    field: str,
    fields: frozenset[str],
    *,
    required: frozenset[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotImportError(f"snapshot {field} must be an object")
    required_fields = fields if required is None else required
    actual = frozenset(value)
    missing = sorted(required_fields - actual)
    unknown = sorted(actual - fields)
    if missing or unknown:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if unknown:
            parts.append("unknown " + ", ".join(unknown))
        raise SnapshotImportError(f"snapshot {field} fields do not match schema: " + "; ".join(parts))
    return value


def _public_text(value: Any, field: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SnapshotImportError(f"snapshot {field} must be text")
    if not allow_empty and not value.strip():
        raise SnapshotImportError(f"snapshot {field} must not be empty")
    if len(value) > limit or any(
        ord(char) < 0x20 or 0xD800 <= ord(char) <= 0xDFFF for char in value
    ):
        raise SnapshotImportError(f"snapshot {field} is not bounded public text")
    if _SECRET_ASSIGNMENT.search(value) or _TOKEN_LITERAL.search(value) or _INTERNAL_PATH.search(value):
        raise SnapshotImportError(f"snapshot {field} contains a private literal")
    return value


def _token(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SnapshotImportError(f"snapshot {field} is not an allowed token")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise SnapshotImportError(f"snapshot {field} must be boolean")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SAFE_INTEGER:
        raise SnapshotImportError(f"snapshot {field} must be a non-negative safe integer")
    return value


def _finite_number(value: Any, field: str, *, minimum: float = 0.0) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotImportError(f"snapshot {field} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < minimum or abs(numeric) > MAX_SAFE_INTEGER:
        raise SnapshotImportError(f"snapshot {field} is outside the accepted numeric range")
    return value


def _timestamp(value: Any, field: str, *, now: float, required: bool) -> int | float | None:
    if value is None and not required:
        return None
    numeric_value = _finite_number(value, field, minimum=EARLIEST_TIMESTAMP)
    if float(numeric_value) > now + MAX_FUTURE_SKEW_SECONDS:
        raise SnapshotImportError(f"snapshot {field} is more than five minutes in the future")
    return numeric_value


def _public_url(value: Any, field: str) -> str:
    text = _public_text(value, field, 4096, allow_empty=True)
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SnapshotImportError(f"snapshot {field} URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise SnapshotImportError(f"snapshot {field} URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise SnapshotImportError(f"snapshot {field} URL contains private components")
    if port is not None and not 1 <= port <= 65535:
        raise SnapshotImportError(f"snapshot {field} URL has an invalid port")
    host_token = host.lower().rstrip(".")
    if host_token == "localhost" or host_token.endswith((".local", ".internal", ".localhost")):
        raise SnapshotImportError(f"snapshot {field} URL is not public")
    try:
        address = ipaddress.ip_address(host_token)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise SnapshotImportError(f"snapshot {field} URL is not public")
    return text


def _samples(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 3:
        raise SnapshotImportError(f"snapshot {field} must be a bounded array")
    projected = []
    for index, item in enumerate(value):
        name = f"{field}[{index}]"
        row = _closed_object(
            item, name, SAMPLE_FIELDS, required=SAMPLE_REQUIRED_FIELDS)
        clean: dict[str, Any] = {
            "title": _public_text(row["title"], f"{name}.title", 240, allow_empty=True),
            "url": _public_url(row["url"], f"{name}.url"),
        }
        if "source" in row:
            clean["source"] = _public_text(row["source"], f"{name}.source", 80)
        if "matched_in" in row:
            modes = row["matched_in"]
            if not isinstance(modes, list) or len(modes) > 2 or any(
                mode not in {"tag", "text"} for mode in modes
            ):
                raise SnapshotImportError(f"snapshot {name}.matched_in is unsupported")
            clean["matched_in"] = list(modes)
        if "deletion_signal" in row:
            clean["deletion_signal"] = _public_text(
                row["deletion_signal"], f"{name}.deletion_signal", 80)
        projected.append(clean)
    return projected


def _run(value: Any, field: str, *, now: float) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _closed_object(value, field, RUN_FIELDS)
    status = row["status"]
    if status not in {"running", "ok", "degraded", "error", "unknown"}:
        raise SnapshotImportError(f"snapshot {field}.status is unsupported")
    return {
        "id": _nonnegative_int(row["id"], f"{field}.id"),
        "started_at": _timestamp(row["started_at"], f"{field}.started_at", now=now, required=False),
        "finished_at": _timestamp(row["finished_at"], f"{field}.finished_at", now=now, required=False),
        "status": status,
        "healthy": _boolean(row["healthy"], f"{field}.healthy"),
        "duration_s": _finite_number(row["duration_s"], f"{field}.duration_s"),
        "collection_errors": _nonnegative_int(
            row["collection_errors"], f"{field}.collection_errors"),
    }


def _health(
    value: Any,
    *,
    now: float,
    generated_at: float,
    top_status: str,
) -> dict[str, Any]:
    health = _closed_object(value, "health", HEALTH_FIELDS)
    if health["status"] != top_status:
        raise SnapshotImportError("snapshot health status disagrees with top-level status")
    live = _boolean(health["live"], "health.live")
    ready = _boolean(health["ready"], "health.ready")
    stale = _boolean(health["stale"], "health.stale")
    if not live:
        raise SnapshotImportError("snapshot exporter is not live")
    if top_status == "ok" and (not ready or stale):
        raise SnapshotImportError("snapshot status ok requires ready, non-stale health")
    if top_status != "ok" and ready:
        raise SnapshotImportError("snapshot non-ok status cannot claim ready health")
    reasons = health["reasons"]
    if not isinstance(reasons, list) or len(reasons) > 16:
        raise SnapshotImportError("snapshot health.reasons must be a bounded array")
    projected_reasons = [
        _public_text(reason, f"health.reasons[{index}]", 80)
        for index, reason in enumerate(reasons)
    ]
    freshness = _closed_object(health["freshness"], "health.freshness", FRESHNESS_FIELDS)
    freshness_status = freshness["status"]
    if freshness_status not in {"empty", "partial", "stale", "fresh"}:
        raise SnapshotImportError("snapshot health.freshness.status is unsupported")
    core_data_at = _timestamp(
        freshness["core_data_at"], "health.freshness.core_data_at",
        now=now, required=top_status == "ok")
    age_seconds = None if freshness["age_seconds"] is None else _finite_number(
        freshness["age_seconds"], "health.freshness.age_seconds")
    stale_after_seconds = _nonnegative_int(
        freshness["stale_after_seconds"], "health.freshness.stale_after_seconds")
    if core_data_at is None:
        if age_seconds is not None or freshness_status not in {"empty", "partial"}:
            raise SnapshotImportError("snapshot freshness has no evidence timestamp but claims an age")
    else:
        reported_age = max(0.0, generated_at - float(core_data_at))
        if age_seconds is None or abs(float(age_seconds) - reported_age) > 1.0:
            raise SnapshotImportError("snapshot reported evidence age disagrees with its timestamps")
        if top_status == "ok" and max(0.0, now - float(core_data_at)) > stale_after_seconds:
            raise SnapshotImportError("snapshot ok evidence is older than its declared freshness ceiling")
    if freshness_status == "fresh" and stale:
        raise SnapshotImportError("snapshot fresh status disagrees with stale health")
    if freshness_status in {"empty", "partial", "stale"} and not stale:
        raise SnapshotImportError("snapshot non-fresh status disagrees with non-stale health")
    return {
        "status": top_status,
        "live": live,
        "ready": ready,
        "stale": stale,
        "posts": _nonnegative_int(health["posts"], "health.posts"),
        "reasons": projected_reasons,
        "freshness": {
            "status": freshness_status,
            "core_data_at": core_data_at,
            "age_seconds": age_seconds,
            "stale_after_seconds": stale_after_seconds,
        },
        "last_run": _run(health["last_run"], "health.last_run", now=now),
        "last_completed_run": _run(
            health["last_completed_run"], "health.last_completed_run", now=now),
        "last_successful_run_at": _timestamp(
            health["last_successful_run_at"], "health.last_successful_run_at",
            now=now, required=False),
    }


def _cycle(value: Any, *, now: float) -> dict[str, Any]:
    cycle = _closed_object(value, "coverage.last_successful_cycle", CYCLE_FIELDS)
    raw_sources = cycle["sources"]
    if not isinstance(raw_sources, list) or len(raw_sources) > 100:
        raise SnapshotImportError("snapshot coverage.last_successful_cycle.sources is invalid")
    sources = []
    for index, raw in enumerate(raw_sources):
        field = f"coverage.last_successful_cycle.sources[{index}]"
        row = _closed_object(raw, field, CYCLE_SOURCE_FIELDS)
        status = row["status"]
        if status not in {"ok", "partial", "error"}:
            raise SnapshotImportError(f"snapshot {field}.status is unsupported")
        sources.append({
            "name": _public_text(row["name"], f"{field}.name", 80),
            "status": status,
            "new": _nonnegative_int(row["new"], f"{field}.new"),
            "dupes": _nonnegative_int(row["dupes"], f"{field}.dupes"),
            "attempted": _nonnegative_int(row["attempted"], f"{field}.attempted"),
            "succeeded": _nonnegative_int(row["succeeded"], f"{field}.succeeded"),
            "failed": _nonnegative_int(row["failed"], f"{field}.failed"),
        })
    projected = {
        "new": _nonnegative_int(cycle["new"], "coverage.last_successful_cycle.new"),
        "dupes": _nonnegative_int(cycle["dupes"], "coverage.last_successful_cycle.dupes"),
        "errors": _nonnegative_int(cycle["errors"], "coverage.last_successful_cycle.errors"),
        "attempted": _nonnegative_int(
            cycle["attempted"], "coverage.last_successful_cycle.attempted"),
        "succeeded": _nonnegative_int(
            cycle["succeeded"], "coverage.last_successful_cycle.succeeded"),
        "failed": _nonnegative_int(
            cycle["failed"], "coverage.last_successful_cycle.failed"),
        "posts_scored": _nonnegative_int(
            cycle["posts_scored"], "coverage.last_successful_cycle.posts_scored"),
        "sources": sources,
    }
    for source in sources:
        if source["succeeded"] + source["failed"] != source["attempted"]:
            raise SnapshotImportError("snapshot source attempt counts disagree")
    return projected


def _coverage(value: Any, *, now: float) -> dict[str, Any]:
    coverage = _closed_object(value, "coverage", COVERAGE_FIELDS)
    if coverage["completeness"] != "not_measured":
        raise SnapshotImportError("snapshot coverage.completeness is unsupported")
    raw_sources = coverage["observed_sources"]
    if not isinstance(raw_sources, list) or len(raw_sources) > 100:
        raise SnapshotImportError("snapshot coverage.observed_sources must be a bounded array")
    sources = []
    for index, raw in enumerate(raw_sources):
        field = f"coverage.observed_sources[{index}]"
        row = _closed_object(raw, field, OBSERVED_SOURCE_FIELDS)
        sources.append({
            "name": _public_text(row["name"], f"{field}.name", 80),
            "posts": _nonnegative_int(row["posts"], f"{field}.posts"),
            "first_published_at": _timestamp(
                row["first_published_at"], f"{field}.first_published_at", now=now, required=False),
            "latest_published_at": _timestamp(
                row["latest_published_at"], f"{field}.latest_published_at", now=now, required=False),
            "latest_fetched_at": _timestamp(
                row["latest_fetched_at"], f"{field}.latest_fetched_at", now=now, required=False),
        })
    return {
        "completeness": "not_measured",
        "scope": _public_text(coverage["scope"], "coverage.scope", 500),
        "observed_source_count": _nonnegative_int(
            coverage["observed_source_count"], "coverage.observed_source_count"),
        "observed_sources": sources,
        "first_published_at": _timestamp(
            coverage["first_published_at"], "coverage.first_published_at", now=now, required=False),
        "latest_published_at": _timestamp(
            coverage["latest_published_at"], "coverage.latest_published_at", now=now, required=False),
        "latest_fetched_at": _timestamp(
            coverage["latest_fetched_at"], "coverage.latest_fetched_at", now=now, required=False),
        "last_successful_cycle": _cycle(coverage["last_successful_cycle"], now=now),
    }


def _ddti(value: Any, *, now: float) -> dict[str, Any] | None:
    if value is None:
        return None
    ddti = _closed_object(value, "ddti", DDTI_FIELDS)
    raw_domains = ddti["by_domain"]
    if not isinstance(raw_domains, dict) or len(raw_domains) > 64:
        raise SnapshotImportError("snapshot ddti.by_domain must be a closed domain map")
    by_domain = {}
    for domain, count in raw_domains.items():
        clean_domain = _public_text(domain, "ddti.by_domain key", 64)
        by_domain[clean_domain] = _nonnegative_int(count, f"ddti.by_domain.{clean_domain}")
    raw_ranked = ddti["ranked"]
    if not isinstance(raw_ranked, list) or len(raw_ranked) > 500:
        raise SnapshotImportError("snapshot ddti.ranked must be a bounded array")
    ranked = []
    for index, raw in enumerate(raw_ranked):
        field = f"ddti.ranked[{index}]"
        row = _closed_object(raw, field, DDTI_ROW_FIELDS)
        domain = _public_text(row["domain"], f"{field}.domain", 64)
        ranked.append({
            "term": _public_text(row["term"], f"{field}.term", 160),
            "domain": domain,
            "attention": _finite_number(row["attention"], f"{field}.attention"),
            "novelty": _finite_number(row["novelty"], f"{field}.novelty"),
            "threat": _finite_number(row["threat"], f"{field}.threat"),
            "is_new": _boolean(row["is_new"], f"{field}.is_new"),
            "total": _nonnegative_int(row["total"], f"{field}.total"),
            "recent": _nonnegative_int(row["recent"], f"{field}.recent"),
            "tag_observations": _nonnegative_int(
                row["tag_observations"], f"{field}.tag_observations"),
            "text_observations": _nonnegative_int(
                row["text_observations"], f"{field}.text_observations"),
            "direct_signal_observations": _nonnegative_int(
                row["direct_signal_observations"], f"{field}.direct_signal_observations"),
            "samples": _samples(row["samples"], f"{field}.samples"),
        })
    return {
        "generated_at": _timestamp(ddti["generated_at"], "ddti.generated_at", now=now, required=True),
        "n_posts": _nonnegative_int(ddti["n_posts"], "ddti.n_posts"),
        "n_posts_scored": _nonnegative_int(ddti["n_posts_scored"], "ddti.n_posts_scored"),
        "n_terms": _nonnegative_int(ddti["n_terms"], "ddti.n_terms"),
        "by_domain": by_domain,
        "ranked": ranked,
    }


def _economic(value: Any, *, now: float) -> dict[str, Any] | None:
    if value is None:
        return None
    economic = _closed_object(value, "economic", ECONOMIC_FIELDS)
    raw_ranked = economic["ranked"]
    if not isinstance(raw_ranked, list) or len(raw_ranked) > 250:
        raise SnapshotImportError("snapshot economic.ranked must be a bounded array")
    ranked = []
    for index, raw in enumerate(raw_ranked):
        field = f"economic.ranked[{index}]"
        row = _closed_object(raw, field, ECONOMIC_ROW_FIELDS)
        ranked.append({
            "term": _public_text(row["term"], f"{field}.term", 160),
            "weight": _finite_number(row["weight"], f"{field}.weight"),
            "samples": _samples(row["samples"], f"{field}.samples"),
        })
    metric_name = _token(economic["metric_name"], "economic.metric_name", METHOD_TOKEN)
    if metric_name != "observed_economic_topic_share":
        raise SnapshotImportError("snapshot economic.metric_name is unsupported")
    pct = _nonnegative_int(economic["pct"], "economic.pct")
    if pct > 100:
        raise SnapshotImportError("snapshot economic.pct must not exceed 100")
    return {
        "generated_at": _timestamp(
            economic["generated_at"], "economic.generated_at", now=now, required=True),
        "metric_name": metric_name,
        "scope": _public_text(economic["scope"], "economic.scope", 500),
        "pct": pct,
        "n_econ_articles": _nonnegative_int(
            economic["n_econ_articles"], "economic.n_econ_articles"),
        "ranked": ranked,
    }


def _leads(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 100:
        raise SnapshotImportError("snapshot leads must be a bounded array")
    projected = []
    for index, raw in enumerate(value):
        field = f"leads[{index}]"
        row = _closed_object(raw, field, LEAD_FIELDS)
        domain = _public_text(row["domain"], f"{field}.domain", 64)
        projected.append({
            "term": _public_text(row["term"], f"{field}.term", 160),
            "domain": domain,
            "score": _finite_number(row["score"], f"{field}.score"),
            "threat": _finite_number(row["threat"], f"{field}.threat"),
            "novelty": _finite_number(row["novelty"], f"{field}.novelty"),
            "n": _nonnegative_int(row["n"], f"{field}.n"),
            "is_new": _boolean(row["is_new"], f"{field}.is_new"),
            "samples": _samples(row["samples"], f"{field}.samples"),
        })
    return projected


def validate_snapshot(document: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Validate and reconstruct the closed public-export document."""
    checked_at = time.time() if now is None else float(now)
    _bounded_shape(document)
    root = _closed_object(document, "root", TOP_LEVEL_FIELDS)
    if root["schema"] != SCHEMA or root["schema_version"] != SCHEMA_VERSION:
        raise SnapshotImportError("snapshot schema identity or version is unsupported")
    if root["source"] != SOURCE:
        raise SnapshotImportError("snapshot source identity is unsupported")
    if root["method"] != EXPECTED_METHOD or root["method_version"] != EXPECTED_METHOD_VERSION:
        raise SnapshotImportError("snapshot method identity or version is unsupported")
    status = root["status"]
    if status not in STATUSES:
        raise SnapshotImportError("snapshot status is unsupported")

    methods = _closed_object(root["methods"], "methods", METHOD_FIELDS)
    projected_methods = {
        name: _token(methods[name], f"methods.{name}", METHOD_TOKEN)
        for name in sorted(METHOD_FIELDS)
    }
    if projected_methods != EXPECTED_METHODS:
        raise SnapshotImportError("snapshot component method versions are unsupported")
    generated_at = _timestamp(
        root["generated_at"], "generated_at", now=checked_at, required=True)
    data_timestamp = _timestamp(
        root["data_timestamp"], "data_timestamp", now=checked_at, required=status == "ok")
    if data_timestamp is not None and data_timestamp > generated_at + MAX_FUTURE_SKEW_SECONDS:
        raise SnapshotImportError("snapshot data_timestamp is later than its export time")

    raw_timestamps = _closed_object(root["timestamps"], "timestamps", TIMESTAMP_FIELDS)
    timestamps = {
        name: _timestamp(
            raw_timestamps[name], f"timestamps.{name}", now=checked_at, required=False)
        for name in sorted(TIMESTAMP_FIELDS)
    }
    health = _health(
        root["health"], now=checked_at, generated_at=float(generated_at), top_status=status)
    if health["freshness"]["core_data_at"] != data_timestamp:
        raise SnapshotImportError("snapshot core evidence timestamps disagree")
    core_values = [
        timestamps["ddti_generated_at"], timestamps["economic_generated_at"]
    ]
    available_core = [value for value in core_values if value is not None]
    expected_core = min(available_core) if available_core else None
    if data_timestamp != expected_core:
        raise SnapshotImportError("snapshot data timestamp does not match its core inputs")
    missing_core = sum(value is None for value in core_values)
    expected_age = (
        round(max(0.0, float(generated_at) - float(expected_core)), 3)
        if expected_core is not None else None
    )
    if health["freshness"]["age_seconds"] != expected_age:
        raise SnapshotImportError("snapshot freshness age does not match its core inputs")
    if missing_core == len(core_values):
        expected_freshness = "empty"
    elif missing_core:
        expected_freshness = "partial"
    elif expected_age > health["freshness"]["stale_after_seconds"]:
        expected_freshness = "stale"
    else:
        expected_freshness = "fresh"
    if health["freshness"]["status"] != expected_freshness:
        raise SnapshotImportError("snapshot freshness status does not match its core inputs")
    expected_stale = bool(missing_core) or expected_freshness == "stale"
    if health["stale"] is not expected_stale:
        raise SnapshotImportError("snapshot stale flag does not match its core inputs")
    if health["ready"] is not (status == "ok"):
        raise SnapshotImportError("snapshot readiness does not match its status")
    coverage = _coverage(root["coverage"], now=checked_at)
    scope = _public_text(root["scope"], "scope", 500)
    if scope != coverage["scope"]:
        raise SnapshotImportError("snapshot scope disagrees with coverage scope")

    counts_raw = _closed_object(root["counts"], "counts", COUNT_FIELDS)
    counts = {
        name: _nonnegative_int(counts_raw[name], f"counts.{name}")
        for name in sorted(COUNT_FIELDS)
    }
    ddti = _ddti(root["ddti"], now=checked_at)
    economic = _economic(root["economic"], now=checked_at)
    leads = _leads(root["leads"])

    if counts["posts"] != health["posts"]:
        raise SnapshotImportError("snapshot post counts disagree")
    if counts["sources"] != coverage["observed_source_count"] or counts["sources"] != len(
        coverage["observed_sources"]
    ):
        raise SnapshotImportError("snapshot source counts disagree")
    if counts["topics"] != (ddti["n_terms"] if ddti else 0):
        raise SnapshotImportError("snapshot topic counts disagree")
    if counts["economic_articles"] != (economic["n_econ_articles"] if economic else 0):
        raise SnapshotImportError("snapshot economic counts disagree")
    if counts["leads"] != len(leads):
        raise SnapshotImportError("snapshot lead counts disagree")

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "method": EXPECTED_METHOD,
        "method_version": EXPECTED_METHOD_VERSION,
        "scope": scope,
        "status": status,
        "methods": projected_methods,
        "generated_at": generated_at,
        "data_timestamp": data_timestamp,
        "timestamps": timestamps,
        "health": health,
        "coverage": coverage,
        "counts": counts,
        "ddti": ddti,
        "economic": economic,
        "leads": leads,
    }


def serialize_snapshot(document: dict[str, Any]) -> bytes:
    try:
        return (json.dumps(
            document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SnapshotImportError("snapshot cannot be serialized canonically") from exc


def write_atomic(document: dict[str, Any], output: Path = DEFAULT_OUTPUT) -> None:
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    data = serialize_snapshot(document)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = ""
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def reject_rollback(document: dict[str, Any], output: Path) -> None:
    """Refuse a signed but older snapshot than the publication high-water mark."""
    target = Path(output)
    if not target.exists():
        return
    try:
        previous_bytes = target.read_bytes()
        previous_raw = _parse_document(previous_bytes)
        previous_generated_raw = _finite_number(
            previous_raw.get("generated_at"), "existing.generated_at",
            minimum=EARLIEST_TIMESTAMP)
        previous = validate_snapshot(previous_raw, now=float(previous_generated_raw))
    except (OSError, SnapshotImportError) as exc:
        raise SnapshotImportError("existing snapshot high-water mark is unreadable") from exc
    previous_generated = float(previous["generated_at"])
    incoming_generated = float(document["generated_at"])
    if incoming_generated < previous_generated:
        raise SnapshotImportError("snapshot generated_at would roll back the published high-water mark")
    if incoming_generated == previous_generated and serialize_snapshot(document) != previous_bytes:
        raise SnapshotImportError("snapshot generation timestamp equivocates with published bytes")
    previous_data = previous["data_timestamp"]
    if previous_data is not None and (
        document["data_timestamp"] is None
        or float(document["data_timestamp"]) < float(previous_data)
    ):
        raise SnapshotImportError("snapshot data_timestamp would roll back published evidence")


def _download(fetcher: Fetcher, endpoint: str, *, max_bytes: int, accept: str) -> bytes:
    try:
        payload = fetcher(
            endpoint,
            max_bytes=max_bytes,
            timeout=TIMEOUT_SECONDS,
            max_redirects=0,
            headers={
                "Accept": accept,
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
    except (FetchError, OSError, TimeoutError) as exc:
        raise SnapshotImportError(
            f"snapshot download failed ({type(exc).__name__})") from exc
    if not isinstance(payload, bytes):
        raise SnapshotImportError("snapshot fetch must return raw bytes")
    return payload


def _authenticated_bytes(
    endpoint: str,
    key: bytes,
    *,
    fetcher: Fetcher,
) -> bytes:
    sidecar = signature_url(endpoint)
    for attempt in range(PAIR_ATTEMPTS):
        payload = _download(fetcher, endpoint, max_bytes=MAX_BYTES, accept="application/json")
        signature = _download(
            fetcher, sidecar, max_bytes=MAX_SIGNATURE_BYTES, accept="text/plain")
        match = SIGNATURE.fullmatch(signature)
        if match is None:
            raise SnapshotImportError("snapshot signature sidecar is malformed")
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest().encode("ascii")
        if hmac.compare_digest(match.group(1), expected):
            return payload
        if attempt + 1 == PAIR_ATTEMPTS:
            break
    raise SnapshotImportError("snapshot signature did not match after bounded pair refetch")


def import_snapshot(
    url: str,
    *,
    hmac_key: str | bytes,
    output: Path = DEFAULT_OUTPUT,
    fetcher: Fetcher = safe_fetch_bytes,
    now: float | None = None,
) -> dict[str, Any]:
    endpoint = validate_https_url(url)
    key = _hmac_key(hmac_key)
    payload = _authenticated_bytes(endpoint, key, fetcher=fetcher)
    parsed = _parse_document(payload)
    if serialize_snapshot(parsed) != payload:
        raise SnapshotImportError("snapshot bytes are not canonical JSON")
    document = validate_snapshot(parsed, now=now)
    reject_rollback(document, output)
    write_atomic(document, output)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get(URL_ENV, ""),
                        help=f"HTTPS public snapshot URL (default: ${URL_ENV})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="atomic destination for the validated public snapshot")
    args = parser.parse_args(argv)
    if not args.url:
        print(f"snapshot import skipped: {URL_ENV} is unset (optional source remains absent)")
        return 0
    try:
        document = import_snapshot(
            args.url,
            hmac_key=os.environ.get(HMAC_KEY_ENV, ""),
            output=args.output,
        )
    except SnapshotImportError as exc:
        print(f"snapshot import failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"snapshot import -> {args.output} · status={document['status']} · "
        f"data_timestamp={document['data_timestamp']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
