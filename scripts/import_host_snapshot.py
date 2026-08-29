"""Import sanitized Hetzner readings into the public Git tree.

The collectors run on the fixed German vantage. GitHub Actions must not invent a
second measurement. This boundary fetches only the exact-path Caddy publications
under api.seiche.info, with redirects disabled, and writes an atomic last-good
replacement. Origins are code constants: changing a URL requires review.

Every successful comparison emits one JSON outcome. A reviewed batch may retain a
newer validated local document when a host lags, but equivocation and invalid evidence
always fail closed.

Usage:  PYTHONPATH=. python -m scripts.import_host_snapshot \
          --allow-empty-bootstrap-404 --keep-last-good-on-stale
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable

from core.safe_fetch import FetchError, safe_fetch_bytes


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
TIMEOUT_SECONDS = 15.0
EARLIEST = datetime(2025, 1, 1, tzinfo=timezone.utc)
MAX_FUTURE_SKEW_SECONDS = 300.0
EVIDENCE_LAKE_RECEIPT_FILENAME = "evidence-lake-metrics-producer-receipt.json"
EVIDENCE_LAKE_RECEIPT_URL = (
    "https://api.seiche.info/palimpsest/evidence-lake-metrics/"
    + EVIDENCE_LAKE_RECEIPT_FILENAME
)
EVIDENCE_LAKE_RECEIPT_MAX_BYTES = 16 * 1024
EVIDENCE_LAKE_RECEIPT_SCHEMA = "bulk.public-metrics-producer-receipt.v1"
EVIDENCE_LAKE_RECEIPT_KEY_ID = "neo-public-metrics-2026-08"
EVIDENCE_LAKE_RECEIPT_KEY_ENV = "EVIDENCE_LAKE_METRICS_HMAC_KEY"
EVIDENCE_LAKE_PRODUCER_ID = "palimpsest-bulk-data-plane"
EVIDENCE_LAKE_PRODUCER_RELEASE_ID = (
    "f7a422c13521ab6b21325c9eac04ef1799c94f0c9ee71d2116a3c8aedca89f41"
)
EVIDENCE_LAKE_MIN_KEY_BYTES = 32
EVIDENCE_LAKE_MAX_KEY_BYTES = 4 * 1024
EVIDENCE_LAKE_MAX_RECEIPT_ATTEMPTS = 3
EVIDENCE_LAKE_RFC3339_RE = re.compile(
    r"(?P<second>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z\Z"
)

Fetcher = Callable[..., bytes]


class HostSnapshotImportError(RuntimeError):
    """A Hetzner host snapshot failed closed-schema import."""


@dataclass(frozen=True)
class HostSnapshot:
    """One exact-path publication the public importer may fetch."""

    snapshot_id: str
    url: str
    filename: str
    max_bytes: int
    required_fields: tuple[str, ...]


@dataclass(frozen=True)
class ImportOutcome:
    """Observable result of comparing one host publication with its high-water mark."""

    snapshot_id: str
    status: str
    incoming_generated_at: str | None
    retained_generated_at: str | None
    incoming_sha256: str | None
    retained_sha256: str | None
    wrote: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# The Evidence Lake route is code-pinned and activated only through the reviewed
# ``PENDING_SNAPSHOTS`` tuple below. Keeping the activation tuple explicit preserves a
# one-line audit trail while the normal strict 404 and shared-secret gates remain in
# force.
EVIDENCE_LAKE_SNAPSHOT = HostSnapshot(
    snapshot_id="evidence-lake-metrics",
    url=(
        "https://api.seiche.info/palimpsest/evidence-lake-metrics/"
        "evidence-lake-metrics-latest.json"
    ),
    filename="evidence-lake-metrics-latest.json",
    max_bytes=64 * 1024,
    required_fields=(
        "schema",
        "generated_at",
        "edition",
        "summary",
        "lanes",
        "gates",
        "metrics_sha256",
    ),
)
EVIDENCE_LAKE_RECEIPT = HostSnapshot(
    snapshot_id="evidence-lake-metrics-producer-receipt",
    url=EVIDENCE_LAKE_RECEIPT_URL,
    filename=EVIDENCE_LAKE_RECEIPT_FILENAME,
    max_bytes=EVIDENCE_LAKE_RECEIPT_MAX_BYTES,
    required_fields=(),
)
PENDING_SNAPSHOTS: tuple[HostSnapshot, ...] = (EVIDENCE_LAKE_SNAPSHOT,)


# Deliberately not environment or CLI. Changing or activating a trust origin is a
# code review; this explicit tuple append is the reviewed activation gate.
SNAPSHOTS: tuple[HostSnapshot, ...] = (
    HostSnapshot(
        snapshot_id="baike-public-snapshot",
        url=(
            "https://api.seiche.info/palimpsest/baike-public-snapshot/"
            "baike-public-snapshot-latest.json"
        ),
        filename="baike-public-snapshot-latest.json",
        max_bytes=256 * 1024,
        required_fields=(
            "generated_at",
            "source",
            "method",
            "scope",
            "n_pages",
            "n_ok",
            "n_observations",
        ),
    ),
    HostSnapshot(
        snapshot_id="peer-context",
        url="https://api.seiche.info/palimpsest/peer-context/peer-context-latest.json",
        filename="peer-context-latest.json",
        max_bytes=512 * 1024,
        required_fields=("generated_at", "source", "method", "scope", "n_hosts"),
    ),
    HostSnapshot(
        snapshot_id="greatfire-context",
        url=(
            "https://api.seiche.info/palimpsest/greatfire-context/"
            "greatfire-context-latest.json"
        ),
        filename="greatfire-context-latest.json",
        max_bytes=256 * 1024,
        required_fields=("generated_at", "source", "method", "scope", "n_urls_queried"),
    ),
    HostSnapshot(
        snapshot_id="public-deletion-ledgers",
        url=(
            "https://api.seiche.info/palimpsest/public-deletion-ledgers/"
            "public-deletion-ledgers-latest.json"
        ),
        filename="public-deletion-ledgers-latest.json",
        max_bytes=512 * 1024,
        required_fields=("generated_at", "source", "method", "scope", "n_observations"),
    ),
) + PENDING_SNAPSHOTS


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]], *, snapshot_id: str
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise HostSnapshotImportError(f"{snapshot_id} repeats JSON key {key!r}")
        document[key] = value
    return document


def _reject_constant(value: str, *, snapshot_id: str) -> None:
    raise HostSnapshotImportError(
        f"{snapshot_id} contains non-finite JSON number {value}"
    )


def _parse_finite_float(value: str, *, snapshot_id: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _reject_constant(value, snapshot_id=snapshot_id)
    return parsed


def _parse_json(payload: bytes, *, snapshot_id: str) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise HostSnapshotImportError(f"{snapshot_id} fetch must return raw bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostSnapshotImportError(f"{snapshot_id} is not strict UTF-8") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=lambda pairs: _reject_duplicate_keys(
                pairs, snapshot_id=snapshot_id
            ),
            parse_constant=lambda value: _reject_constant(
                value, snapshot_id=snapshot_id
            ),
            parse_float=lambda value: _parse_finite_float(
                value, snapshot_id=snapshot_id
            ),
        )
    except HostSnapshotImportError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise HostSnapshotImportError(f"{snapshot_id} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise HostSnapshotImportError(f"{snapshot_id} root must be an object")
    return document


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_generated_at(value: Any, *, snapshot_id: str, now: float) -> datetime:
    if not isinstance(value, str):
        raise HostSnapshotImportError(
            f"{snapshot_id} generated_at must be an ISO-8601 string"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HostSnapshotImportError(
            f"{snapshot_id} generated_at is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise HostSnapshotImportError(f"{snapshot_id} generated_at must be UTC")
    parsed = parsed.astimezone(timezone.utc)
    epoch = parsed.timestamp()
    if epoch < EARLIEST.timestamp() or epoch > now + MAX_FUTURE_SKEW_SECONDS:
        raise HostSnapshotImportError(
            f"{snapshot_id} generated_at is outside the accepted clock"
        )
    return parsed


def _fail(spec: HostSnapshot, message: str) -> None:
    raise HostSnapshotImportError(f"{spec.snapshot_id} {message}")


def _require_exact_keys(
    document: dict[str, Any],
    expected: frozenset[str],
    spec: HostSnapshot,
    *,
    context: str = "document",
) -> None:
    missing = sorted(expected.difference(document))
    unexpected = sorted(set(document).difference(expected))
    if missing or unexpected:
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unexpected:
            detail.append(f"unexpected {', '.join(unexpected)}")
        _fail(spec, f"{context} shape drifted ({'; '.join(detail)})")


def _nonnegative_int(
    document: dict[str, Any],
    field: str,
    spec: HostSnapshot,
    *,
    context: str = "document",
) -> int:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(spec, f"{context} {field} must be a non-negative integer")
    return value


def _positive_int(
    document: dict[str, Any],
    field: str,
    spec: HostSnapshot,
    *,
    context: str = "document",
) -> int:
    value = _nonnegative_int(document, field, spec, context=context)
    if value < 1:
        _fail(spec, f"{context} {field} must be positive")
    return value


def _object(
    document: dict[str, Any],
    field: str,
    spec: HostSnapshot,
    *,
    context: str = "document",
) -> dict[str, Any]:
    value = document.get(field)
    if not isinstance(value, dict):
        _fail(spec, f"{context} {field} must be an object")
    return value


def _array(
    document: dict[str, Any],
    field: str,
    spec: HostSnapshot,
    *,
    context: str = "document",
) -> list[Any]:
    value = document.get(field)
    if not isinstance(value, list):
        _fail(spec, f"{context} {field} must be an array")
    return value


def _nonempty_string(
    document: dict[str, Any],
    field: str,
    spec: HostSnapshot,
    *,
    context: str = "document",
) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        _fail(spec, f"{context} {field} must be a non-empty string")
    return value


BAIKE_KEYS = frozenset(
    {
        "collector_status",
        "generated_at",
        "method",
        "method_version",
        "n_login_walled",
        "n_observations",
        "n_ok",
        "n_pages",
        "n_unreachable",
        "observations",
        "pages",
        "scope",
        "source",
        "status",
        "valid_for_series",
    }
)
GREATFIRE_KEYS = frozenset(
    {
        "attribution",
        "generated_at",
        "hosts",
        "ledgers",
        "license",
        "method",
        "method_version",
        "n_misses",
        "n_silent",
        "n_urls_queried",
        "n_verdicts",
        "observer_class",
        "schema_version",
        "scope",
        "source",
        "verdicts",
        "window_days",
    }
)
GREATFIRE_LEDGER_KEYS = frozenset(
    {"http_status", "items", "list", "n_items", "status", "url"}
)
GREATFIRE_LEDGER_ITEM_KEYS = frozenset({"path", "status", "title"})
PEER_KEYS = frozenset(
    {
        "cdt_items",
        "disk_estimate",
        "feature_rows",
        "generated_at",
        "greatfire",
        "method",
        "method_version",
        "n_cdt",
        "n_greatfire",
        "n_hosts",
        "n_ooni",
        "observer_class",
        "ooni",
        "schema_version",
        "scope",
        "source",
        "weiboscope",
    }
)
OONI_KEYS = frozenset(
    {
        "attribution",
        "generated_at",
        "hosts",
        "method",
        "method_version",
        "n_hits",
        "n_hosts",
        "n_misses",
        "scope",
        "source",
    }
)
WEIBOSCOPE_KEYS = frozenset(
    {
        "abstention",
        "attribution",
        "citation",
        "doi",
        "dump_on_node",
        "generated_at",
        "index",
        "method",
        "method_version",
        "probes",
        "scope",
        "source",
    }
)
DISK_ESTIMATE_KEYS = frozenset(
    {
        "cdt_excerpts",
        "greatfire_context_json",
        "n_cdt_rows",
        "n_greatfire_rows",
        "n_ooni_rows",
        "ooni_peer_index",
        "weiboscope",
    }
)
DELETION_KEYS = frozenset(
    {
        "generated_at",
        "ledgers",
        "method",
        "method_version",
        "n_feeds",
        "n_feeds_ok",
        "n_observations",
        "observations",
        "scope",
        "source",
    }
)
DELETION_LEDGER_KEYS = frozenset(
    {
        "http_status",
        "kind",
        "n_items",
        "n_observations",
        "name",
        "note",
        "status",
        "url",
    }
)
EVIDENCE_LAKE_KEYS = frozenset(
    {
        "edition",
        "gates",
        "generated_at",
        "lanes",
        "metrics_sha256",
        "schema",
        "summary",
    }
)
EVIDENCE_LAKE_SUMMARY_KEYS = frozenset(
    {
        "analytical_rows",
        "palimpsest_release_files",
        "publication_eligible_rows",
        "telegram_corpus_records",
        "verified_source_bytes",
    }
)
EVIDENCE_LAKE_LANE_KEYS = frozenset(
    {
        "citation",
        "coverage",
        "id",
        "products",
        "publication_eligible_records",
        "publication_state",
        "queryable_records",
    }
)
EVIDENCE_LAKE_CITATION_KEYS = frozenset({"label", "url"})
EVIDENCE_LAKE_GATES = {
    "common_crawl_bodies": "not-collected",
    "crypto_payload_collection": "disabled-pending-data-terms-review",
    "ofr_publication": "review-required",
    "ooni_bulk_collection": "blocked-pending-commercial-and-privacy-review",
    "telegram_corpus_collection": "blocked",
}
EVIDENCE_LAKE_LANES = (
    {
        "id": "world-bank-wdi",
        "products": ["palimpsest", "seiche"],
        "publication_state": "allowed-with-attribution",
        "coverage_keys": frozenset({"economies", "indicators", "period"}),
        "citation": {
            "label": "World Bank, World Development Indicators",
            "url": (
                "https://datacatalog.worldbank.org/search/dataset/0037712/"
                "world-development-indicators"
            ),
        },
    },
    {
        "id": "unodc-ids",
        "products": ["narcoscope", "palimpsest"],
        "publication_state": "allowed-with-citation",
        "coverage_keys": frozenset(
            {
                "countries_or_territories",
                "distinct_exact_content",
                "drug_substances",
                "period",
            }
        ),
        "citation": {
            "label": "UNODC Drugs Monitoring Platform",
            "url": "https://dmpone.unodc.org/downloadIDS",
        },
    },
    {
        "id": "ofr-stfm",
        "products": ["liquilens", "seiche"],
        "publication_state": "review-required",
        "coverage_keys": frozenset({"series"}),
        "citation": {
            "label": "U.S. Office of Financial Research, Short-term Funding Monitor",
            "url": "https://www.financialresearch.gov/short-term-funding-monitor/api/",
        },
    },
    {
        "id": "binance-public-archive",
        "products": ["crypto"],
        "publication_state": "manifest-only-pending-data-terms-review",
        "coverage_keys": frozenset(
            {"collected_payload_files", "verified_manifest_files"}
        ),
        "citation": {
            "label": "Binance public data archive documentation",
            "url": "https://github.com/binance/binance-public-data",
        },
    },
)


def _validate_baike(document: dict[str, Any], spec: HostSnapshot) -> None:
    _require_exact_keys(document, BAIKE_KEYS, spec)
    if _positive_int(document, "method_version", spec) != 1:
        _fail(spec, "method_version is not the reviewed version 1")
    n_pages = _nonnegative_int(document, "n_pages", spec)
    n_ok = _nonnegative_int(document, "n_ok", spec)
    n_unreachable = _nonnegative_int(document, "n_unreachable", spec)
    n_walled = _nonnegative_int(document, "n_login_walled", spec)
    n_observations = _nonnegative_int(document, "n_observations", spec)
    pages = _object(document, "pages", spec)
    observations = _array(document, "observations", spec)
    if len(pages) != n_pages:
        _fail(spec, "n_pages does not match pages")
    if len(observations) != n_observations:
        _fail(spec, "n_observations does not match observations")
    if n_ok + n_unreachable != n_pages:
        _fail(spec, "n_ok + n_unreachable must equal n_pages")
    if n_walled > n_unreachable:
        _fail(spec, "n_login_walled cannot exceed n_unreachable")
    if any(
        not isinstance(url, str) or not isinstance(row, dict)
        for url, row in pages.items()
    ):
        _fail(spec, "pages must map URL strings to objects")
    if any(not isinstance(row, dict) for row in observations):
        _fail(spec, "observations entries must be objects")

    status = document.get("status")
    collector_status = document.get("collector_status")
    valid_for_series = document.get("valid_for_series")
    if type(valid_for_series) is not bool:
        _fail(spec, "valid_for_series must be a boolean")
    expected = (
        ("ok", "observed", True)
        if n_ok > 0
        else ("unreachable", "source_refused", False)
    )
    if (status, collector_status, valid_for_series) != expected:
        _fail(spec, "status, collector_status, and valid_for_series contradict n_ok")


def _validate_greatfire(
    document: dict[str, Any],
    spec: HostSnapshot,
    *,
    context: str = "document",
) -> None:
    _require_exact_keys(document, GREATFIRE_KEYS, spec, context=context)
    if document.get("schema_version") != "palimpsest-greatfire-context/v1":
        _fail(spec, f"{context} schema_version is not the reviewed contract")
    if document.get("observer_class") != "public-ledger":
        _fail(spec, f"{context} observer_class is not public-ledger")
    if _positive_int(document, "method_version", spec, context=context) != 1:
        _fail(spec, f"{context} method_version is not the reviewed version 1")
    if _positive_int(document, "window_days", spec, context=context) != 90:
        _fail(spec, f"{context} window_days is not the reviewed 90-day window")
    n_queried = _nonnegative_int(document, "n_urls_queried", spec, context=context)
    n_verdicts = _nonnegative_int(document, "n_verdicts", spec, context=context)
    n_misses = _nonnegative_int(document, "n_misses", spec, context=context)
    n_silent = _nonnegative_int(document, "n_silent", spec, context=context)
    hosts = _array(document, "hosts", spec, context=context)
    verdicts = _array(document, "verdicts", spec, context=context)
    ledgers = _array(document, "ledgers", spec, context=context)
    if len(hosts) != n_verdicts:
        _fail(spec, f"{context} n_verdicts does not match hosts")
    if len(verdicts) + n_silent != n_queried:
        _fail(spec, f"{context} verdict rows + n_silent must equal n_urls_queried")
    if n_verdicts > len(verdicts):
        _fail(spec, f"{context} n_verdicts cannot exceed verdict rows")
    if n_misses > len(verdicts) - n_verdicts:
        _fail(spec, f"{context} n_misses exceeds non-verdict rows")
    if any(not isinstance(row, dict) for row in hosts + verdicts):
        _fail(spec, f"{context} hosts and verdict entries must be objects")
    for index, raw in enumerate(ledgers):
        if not isinstance(raw, dict):
            _fail(spec, f"{context} ledgers[{index}] must be an object")
        ledger_context = f"{context} ledgers[{index}]"
        _require_exact_keys(raw, GREATFIRE_LEDGER_KEYS, spec, context=ledger_context)
        _nonempty_string(raw, "list", spec, context=ledger_context)
        _nonempty_string(raw, "url", spec, context=ledger_context)
        _nonempty_string(raw, "status", spec, context=ledger_context)
        items = _array(raw, "items", spec, context=ledger_context)
        if _nonnegative_int(raw, "n_items", spec, context=ledger_context) != len(items):
            _fail(spec, f"{context} ledgers[{index}] n_items does not match items")
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                _fail(spec, f"{ledger_context} items[{item_index}] must be an object")
            _require_exact_keys(
                item,
                GREATFIRE_LEDGER_ITEM_KEYS,
                spec,
                context=f"{ledger_context} items[{item_index}]",
            )


def _validate_peer(document: dict[str, Any], spec: HostSnapshot) -> None:
    _require_exact_keys(document, PEER_KEYS, spec)
    if document.get("schema_version") != "palimpsest-peer-context.v1":
        _fail(spec, "schema_version is not the reviewed peer-context contract")
    if document.get("observer_class") != "outside-china-node":
        _fail(spec, "observer_class is not outside-china-node")
    if _positive_int(document, "method_version", spec) != 1:
        _fail(spec, "method_version is not the reviewed version 1")
    n_hosts = _nonnegative_int(document, "n_hosts", spec)
    n_greatfire = _nonnegative_int(document, "n_greatfire", spec)
    n_ooni = _nonnegative_int(document, "n_ooni", spec)
    n_cdt = _nonnegative_int(document, "n_cdt", spec)
    feature_rows = _nonnegative_int(document, "feature_rows", spec)
    cdt_items = _array(document, "cdt_items", spec)
    if len(cdt_items) != n_cdt or any(not isinstance(row, dict) for row in cdt_items):
        _fail(spec, "n_cdt must match object entries in cdt_items")

    greatfire = document.get("greatfire")
    if greatfire is None:
        if n_greatfire != 0:
            _fail(spec, "n_greatfire must be zero when greatfire is null")
        n_greatfire_rows = 0
    elif isinstance(greatfire, dict):
        _validate_greatfire(greatfire, spec, context="greatfire")
        if n_greatfire != greatfire["n_verdicts"]:
            _fail(spec, "n_greatfire does not match greatfire.n_verdicts")
        n_greatfire_rows = len(greatfire["verdicts"])
    else:
        _fail(spec, "greatfire must be an object or null")

    ooni = _object(document, "ooni", spec)
    _require_exact_keys(ooni, OONI_KEYS, spec, context="ooni")
    if _positive_int(ooni, "method_version", spec, context="ooni") != 1:
        _fail(spec, "ooni method_version is not the reviewed version 1")
    ooni_hosts = _nonnegative_int(ooni, "n_hosts", spec, context="ooni")
    ooni_hits = _nonnegative_int(ooni, "n_hits", spec, context="ooni")
    ooni_misses = _nonnegative_int(ooni, "n_misses", spec, context="ooni")
    ooni_rows = _array(ooni, "hosts", spec, context="ooni")
    if len(ooni_rows) != ooni_hosts or ooni_hits + ooni_misses != ooni_hosts:
        _fail(spec, "ooni host/hit/miss counts are inconsistent")
    if n_hosts != ooni_hosts or n_ooni != ooni_hits:
        _fail(spec, "peer host/OONI counts do not match the embedded OONI document")
    if any(not isinstance(row, dict) for row in ooni_rows):
        _fail(spec, "ooni hosts entries must be objects")

    weiboscope = _object(document, "weiboscope", spec)
    _require_exact_keys(weiboscope, WEIBOSCOPE_KEYS, spec, context="weiboscope")
    if _positive_int(weiboscope, "method_version", spec, context="weiboscope") != 1:
        _fail(spec, "weiboscope method_version is not the reviewed version 1")
    if weiboscope.get("dump_on_node") is not False:
        _fail(spec, "weiboscope.dump_on_node must remain false")
    _array(weiboscope, "probes", spec, context="weiboscope")
    _object(weiboscope, "abstention", spec, context="weiboscope")

    disk = _object(document, "disk_estimate", spec)
    _require_exact_keys(disk, DISK_ESTIMATE_KEYS, spec, context="disk_estimate")
    expected_disk_counts = {
        "n_greatfire_rows": n_greatfire_rows,
        "n_ooni_rows": len(ooni_rows),
        "n_cdt_rows": len(cdt_items),
    }
    for field, expected in expected_disk_counts.items():
        if _nonnegative_int(disk, field, spec, context="disk_estimate") != expected:
            _fail(spec, f"disk_estimate.{field} does not match embedded rows")
    if feature_rows != n_greatfire + n_ooni + n_cdt + 1:
        _fail(
            spec,
            "feature_rows must equal live peer rows plus one Weiboscope abstention",
        )


def _validate_deletion_ledgers(document: dict[str, Any], spec: HostSnapshot) -> None:
    _require_exact_keys(document, DELETION_KEYS, spec)
    if _positive_int(document, "method_version", spec) != 1:
        _fail(spec, "method_version is not the reviewed version 1")
    n_feeds = _nonnegative_int(document, "n_feeds", spec)
    n_feeds_ok = _nonnegative_int(document, "n_feeds_ok", spec)
    n_observations = _nonnegative_int(document, "n_observations", spec)
    ledgers = _array(document, "ledgers", spec)
    observations = _array(document, "observations", spec)
    if len(ledgers) != n_feeds:
        _fail(spec, "n_feeds does not match ledgers")
    if len(observations) != n_observations:
        _fail(spec, "n_observations does not match observations")
    if any(not isinstance(row, dict) for row in observations):
        _fail(spec, "observations entries must be objects")

    counted_ok = 0
    counted_observations = 0
    for index, raw in enumerate(ledgers):
        if not isinstance(raw, dict):
            _fail(spec, f"ledgers[{index}] must be an object")
        context = f"ledgers[{index}]"
        _require_exact_keys(raw, DELETION_LEDGER_KEYS, spec, context=context)
        for field in ("name", "url", "kind", "status"):
            _nonempty_string(raw, field, spec, context=context)
        n_items = _nonnegative_int(raw, "n_items", spec, context=context)
        row_observations = _nonnegative_int(
            raw, "n_observations", spec, context=context
        )
        if row_observations > n_items:
            _fail(spec, f"{context} n_observations cannot exceed n_items")
        status = raw["status"]
        http_status = raw.get("http_status")
        expected_status = (
            "ok"
            if http_status == 200 and row_observations > 0
            else "empty-feed"
            if http_status == 200
            else "unreachable"
        )
        if status != expected_status:
            _fail(spec, f"{context} status contradicts HTTP and observation counts")
        counted_ok += status == "ok"
        counted_observations += row_observations
    if counted_ok != n_feeds_ok:
        _fail(spec, "n_feeds_ok does not match ok ledgers")
    if n_feeds_ok == 0:
        _fail(spec, "hollow all-failed ledger publications are not admissible")
    if counted_observations != n_observations:
        _fail(spec, "ledger observation counts do not sum to n_observations")


def _validate_evidence_lake_metrics(
    document: dict[str, Any], spec: HostSnapshot
) -> None:
    _require_exact_keys(document, EVIDENCE_LAKE_KEYS, spec)
    if document.get("schema") != "bulk.public-metrics.v1":
        _fail(spec, "schema is not the reviewed bulk.public-metrics.v1 contract")

    edition = document.get("edition")
    metrics_sha256 = document.get("metrics_sha256")
    if (
        not isinstance(edition, str)
        or len(edition) != 16
        or any(character not in "0123456789abcdef" for character in edition)
    ):
        _fail(spec, "edition must be 16 lowercase hexadecimal characters")
    if (
        not isinstance(metrics_sha256, str)
        or len(metrics_sha256) != 64
        or any(character not in "0123456789abcdef" for character in metrics_sha256)
    ):
        _fail(spec, "metrics_sha256 must be 64 lowercase hexadecimal characters")

    summary = _object(document, "summary", spec)
    _require_exact_keys(summary, EVIDENCE_LAKE_SUMMARY_KEYS, spec, context="summary")
    analytical_rows = _positive_int(summary, "analytical_rows", spec, context="summary")
    eligible_rows = _positive_int(
        summary, "publication_eligible_rows", spec, context="summary"
    )
    _positive_int(summary, "verified_source_bytes", spec, context="summary")
    _positive_int(summary, "palimpsest_release_files", spec, context="summary")
    if (
        _nonnegative_int(summary, "telegram_corpus_records", spec, context="summary")
        != 0
    ):
        _fail(
            spec,
            "summary.telegram_corpus_records must remain zero while collection is blocked",
        )

    lanes = _array(document, "lanes", spec)
    if len(lanes) != len(EVIDENCE_LAKE_LANES):
        _fail(spec, "lanes must contain the four reviewed rows in canonical order")
    counted_analytical = 0
    counted_eligible = 0
    for index, expected in enumerate(EVIDENCE_LAKE_LANES):
        lane = lanes[index]
        context = f"lanes[{index}]"
        if not isinstance(lane, dict):
            _fail(spec, f"{context} must be an object")
        _require_exact_keys(lane, EVIDENCE_LAKE_LANE_KEYS, spec, context=context)
        if lane.get("id") != expected["id"]:
            _fail(spec, f"{context}.id is not the reviewed canonical lane")
        if lane.get("products") != expected["products"]:
            _fail(spec, f"{context}.products drifted from the reviewed product views")
        if lane.get("publication_state") != expected["publication_state"]:
            _fail(spec, f"{context}.publication_state drifted from the reviewed gate")
        queryable = _nonnegative_int(lane, "queryable_records", spec, context=context)
        eligible = _nonnegative_int(
            lane, "publication_eligible_records", spec, context=context
        )
        if eligible > queryable:
            _fail(
                spec, f"{context} publication-eligible records exceed queryable records"
            )
        counted_analytical += queryable
        counted_eligible += eligible

        coverage = _object(lane, "coverage", spec, context=context)
        _require_exact_keys(
            coverage,
            expected["coverage_keys"],
            spec,
            context=f"{context}.coverage",
        )
        citation = _object(lane, "citation", spec, context=context)
        _require_exact_keys(
            citation,
            EVIDENCE_LAKE_CITATION_KEYS,
            spec,
            context=f"{context}.citation",
        )
        if citation != expected["citation"]:
            _fail(spec, f"{context}.citation drifted from the reviewed upstream source")

        if expected["id"] in {"world-bank-wdi", "unodc-ids"}:
            period = _array(coverage, "period", spec, context=f"{context}.coverage")
            if (
                len(period) != 2
                or any(
                    isinstance(year, bool) or not isinstance(year, int)
                    for year in period
                )
                or not 1800 <= period[0] <= period[1] <= 2200
            ):
                _fail(
                    spec, f"{context}.coverage.period must be an ordered two-year range"
                )
            if eligible != queryable:
                _fail(
                    spec, f"{context} reviewed eligible rows must equal queryable rows"
                )
        if expected["id"] == "world-bank-wdi":
            _positive_int(coverage, "economies", spec, context=f"{context}.coverage")
            _positive_int(coverage, "indicators", spec, context=f"{context}.coverage")
        elif expected["id"] == "unodc-ids":
            _positive_int(
                coverage,
                "countries_or_territories",
                spec,
                context=f"{context}.coverage",
            )
            _positive_int(
                coverage, "drug_substances", spec, context=f"{context}.coverage"
            )
            if (
                _positive_int(
                    coverage,
                    "distinct_exact_content",
                    spec,
                    context=f"{context}.coverage",
                )
                > queryable
            ):
                _fail(
                    spec,
                    f"{context}.coverage exact-distinct rows exceed queryable rows",
                )
        elif expected["id"] == "ofr-stfm":
            _positive_int(coverage, "series", spec, context=f"{context}.coverage")
            if eligible != 0:
                _fail(spec, "OFR publication-eligible records must remain zero")
        else:
            _nonnegative_int(
                coverage,
                "verified_manifest_files",
                spec,
                context=f"{context}.coverage",
            )
            if (
                queryable != 0
                or eligible != 0
                or _nonnegative_int(
                    coverage,
                    "collected_payload_files",
                    spec,
                    context=f"{context}.coverage",
                )
                != 0
            ):
                _fail(
                    spec,
                    "crypto lane must remain manifest-only with zero payload records",
                )

    if counted_analytical != analytical_rows:
        _fail(spec, "summary.analytical_rows does not equal the canonical lane sum")
    if counted_eligible != eligible_rows:
        _fail(
            spec,
            "summary.publication_eligible_rows does not equal the canonical lane sum",
        )

    gates = _object(document, "gates", spec)
    _require_exact_keys(gates, frozenset(EVIDENCE_LAKE_GATES), spec, context="gates")
    if gates != EVIDENCE_LAKE_GATES:
        _fail(spec, "gates drifted from the reviewed fail-closed policy")

    digest_payload = {"summary": summary, "lanes": lanes, "gates": gates}
    expected_digest = _sha256(_canonical(digest_payload) + b"\n")
    if metrics_sha256 != expected_digest:
        _fail(spec, "metrics_sha256 does not bind summary, lanes, and gates")
    if edition != expected_digest[:16]:
        _fail(spec, "edition does not match the metrics digest prefix")


SEMANTIC_VALIDATORS: dict[str, Callable[[dict[str, Any], HostSnapshot], None]] = {
    "baike-public-snapshot": _validate_baike,
    "peer-context": _validate_peer,
    "greatfire-context": _validate_greatfire,
    "public-deletion-ledgers": _validate_deletion_ledgers,
    "evidence-lake-metrics": _validate_evidence_lake_metrics,
}


def validate_document(
    document: dict[str, Any],
    spec: HostSnapshot,
    *,
    now: float,
) -> dict[str, Any]:
    missing = [field for field in spec.required_fields if field not in document]
    if missing:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} missing required fields: {', '.join(missing)}"
        )
    _parse_generated_at(document["generated_at"], snapshot_id=spec.snapshot_id, now=now)
    for field in ("source", "method", "scope"):
        if field not in spec.required_fields:
            continue
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} {field} must be a non-empty string"
            )
    for field in spec.required_fields:
        if not field.startswith("n_"):
            continue
        value = document[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} {field} must be a non-negative integer"
            )
    validator = SEMANTIC_VALIDATORS.get(spec.snapshot_id)
    if validator is None:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} has no reviewed semantic validator"
        )
    validator(document, spec)
    return document


def _validated_payload(
    payload: bytes,
    spec: HostSnapshot,
    *,
    now: float,
    source: str,
) -> tuple[dict[str, Any], bytes, datetime]:
    if len(payload) > spec.max_bytes:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} {source} exceeds {spec.max_bytes} bytes"
        )
    document = validate_document(
        _parse_json(payload, snapshot_id=spec.snapshot_id),
        spec,
        now=now,
    )
    canonical = _canonical(document)
    generated_at = _parse_generated_at(
        document["generated_at"],
        snapshot_id=spec.snapshot_id,
        now=now,
    )
    return document, canonical, generated_at


EVIDENCE_LAKE_RECEIPT_KEYS = frozenset(
    {"schema", "projection", "producer", "key_id", "signed_at", "hmac_sha256"}
)
EVIDENCE_LAKE_RECEIPT_PROJECTION_KEYS = frozenset(
    {"sha256", "bytes", "metrics_sha256", "edition", "generated_at"}
)
EVIDENCE_LAKE_RECEIPT_PRODUCER_KEYS = frozenset(
    {"id", "release_id", "release_manifest_sha256", "private_status_sha256"}
)


def _is_lowercase_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _evidence_lake_key() -> bytes:
    value = os.getenv(EVIDENCE_LAKE_RECEIPT_KEY_ENV)
    if value is None:
        raise HostSnapshotImportError(
            "evidence-lake-metrics producer receipt key is unavailable"
        )
    try:
        key = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HostSnapshotImportError(
            "evidence-lake-metrics producer receipt key is not valid UTF-8"
        ) from exc
    if not EVIDENCE_LAKE_MIN_KEY_BYTES <= len(key) <= EVIDENCE_LAKE_MAX_KEY_BYTES:
        raise HostSnapshotImportError(
            "evidence-lake-metrics producer receipt key must contain 32 through 4096 bytes"
        )
    return key


def _parse_receipt_rfc3339(
    value: Any,
    spec: HostSnapshot,
    *,
    context: str,
) -> tuple[datetime, int]:
    match = (
        EVIDENCE_LAKE_RFC3339_RE.fullmatch(value) if isinstance(value, str) else None
    )
    if match is None:
        _fail(spec, f"{context} must use reviewed RFC 3339 UTC syntax")
    try:
        parsed = datetime.strptime(match.group("second"), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} {context} is not a valid RFC 3339 timestamp"
        ) from exc
    fraction = match.group("fraction") or ""
    nanoseconds = int(fraction.ljust(9, "0")) if fraction else 0
    return parsed, nanoseconds


def _validate_receipt_signed_at(
    value: Any, spec: HostSnapshot, *, now: float
) -> tuple[datetime, int]:
    parsed, nanoseconds = _parse_receipt_rfc3339(
        value,
        spec,
        context="producer receipt signed_at",
    )
    epoch_second = int(parsed.timestamp())
    latest = now + MAX_FUTURE_SKEW_SECONDS
    latest_second = math.floor(latest)
    latest_nanoseconds = int((latest - latest_second) * 1_000_000_000)
    if not (
        (int(EARLIEST.timestamp()), 0)
        <= (epoch_second, nanoseconds)
        <= (latest_second, latest_nanoseconds)
    ):
        _fail(spec, "producer receipt signed_at is outside the accepted UTC clock")
    return parsed, nanoseconds


def _validate_evidence_lake_receipt(
    receipt_raw: bytes,
    *,
    projection_raw: bytes,
    projection: dict[str, Any],
    spec: HostSnapshot,
    key: bytes,
    now: float,
) -> dict[str, Any]:
    if not 1 <= len(receipt_raw) <= EVIDENCE_LAKE_RECEIPT_MAX_BYTES:
        _fail(spec, "producer receipt exceeds its byte bound")
    receipt = _parse_json(
        receipt_raw,
        snapshot_id=f"{spec.snapshot_id} producer receipt",
    )
    _require_exact_keys(
        receipt,
        EVIDENCE_LAKE_RECEIPT_KEYS,
        spec,
        context="producer receipt",
    )
    if receipt.get("schema") != EVIDENCE_LAKE_RECEIPT_SCHEMA:
        _fail(spec, "producer receipt schema is not the reviewed contract")
    if receipt.get("key_id") != EVIDENCE_LAKE_RECEIPT_KEY_ID:
        _fail(spec, "producer receipt key_id is not the reviewed key")
    signed_at = _validate_receipt_signed_at(receipt.get("signed_at"), spec, now=now)

    projection_claim = _object(receipt, "projection", spec, context="producer receipt")
    _require_exact_keys(
        projection_claim,
        EVIDENCE_LAKE_RECEIPT_PROJECTION_KEYS,
        spec,
        context="producer receipt projection",
    )
    producer = _object(receipt, "producer", spec, context="producer receipt")
    _require_exact_keys(
        producer,
        EVIDENCE_LAKE_RECEIPT_PRODUCER_KEYS,
        spec,
        context="producer receipt producer",
    )
    for context, value in (
        ("producer receipt projection.sha256", projection_claim.get("sha256")),
        (
            "producer receipt projection.metrics_sha256",
            projection_claim.get("metrics_sha256"),
        ),
        (
            "producer receipt producer.private_status_sha256",
            producer.get("private_status_sha256"),
        ),
        ("producer receipt hmac_sha256", receipt.get("hmac_sha256")),
    ):
        if not _is_lowercase_sha256(value):
            _fail(spec, f"{context} must be 64 lowercase hexadecimal characters")
    if producer.get("id") != EVIDENCE_LAKE_PRODUCER_ID:
        _fail(spec, "producer receipt producer id is not reviewed")
    if (
        producer.get("release_id") != EVIDENCE_LAKE_PRODUCER_RELEASE_ID
        or producer.get("release_manifest_sha256") != EVIDENCE_LAKE_PRODUCER_RELEASE_ID
    ):
        _fail(spec, "producer receipt release identity is not the reviewed release")

    core = {
        field: receipt[field]
        for field in EVIDENCE_LAKE_RECEIPT_KEYS
        if field != "hmac_sha256"
    }
    expected_hmac = hmac.new(
        key,
        _canonical(core) + b"\n",
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(receipt["hmac_sha256"], expected_hmac):
        _fail(spec, "producer receipt HMAC verification failed")
    if receipt_raw != _canonical(receipt) + b"\n":
        _fail(spec, "producer receipt is not canonical JSON with a final newline")

    if projection_raw != _canonical(projection) + b"\n":
        _fail(spec, "public metrics projection is not canonical producer bytes")
    claimed_bytes = _positive_int(
        projection_claim,
        "bytes",
        spec,
        context="producer receipt projection",
    )
    if claimed_bytes != len(projection_raw):
        _fail(spec, "producer receipt projection byte count does not match payload")
    if projection_claim["sha256"] != _sha256(projection_raw):
        _fail(spec, "producer receipt projection digest does not match payload")
    for field in ("generated_at", "edition", "metrics_sha256"):
        if projection_claim.get(field) != projection.get(field):
            _fail(spec, f"producer receipt projection {field} does not match payload")
    projection_at = _parse_receipt_rfc3339(
        projection.get("generated_at"),
        spec,
        context="producer receipt projection.generated_at",
    )
    if signed_at < projection_at:
        _fail(spec, "producer receipt signed_at predates projection.generated_at")
    return receipt


def _download_evidence_lake_pair(
    spec: HostSnapshot,
    fetcher: Fetcher,
) -> tuple[bytes, bytes]:
    for _attempt in range(EVIDENCE_LAKE_MAX_RECEIPT_ATTEMPTS):
        receipt_before = _download(
            EVIDENCE_LAKE_RECEIPT,
            fetcher,
            allow_not_found=False,
        )
        projection = _download(spec, fetcher, allow_not_found=False)
        receipt_after = _download(
            EVIDENCE_LAKE_RECEIPT,
            fetcher,
            allow_not_found=False,
        )
        if receipt_before == receipt_after:
            assert receipt_before is not None and projection is not None
            return projection, receipt_before
    _fail(
        spec,
        "producer receipt changed during all three complete download attempts",
    )


def _read_local_file(path: Path, *, maximum: int, label: str) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise HostSnapshotImportError(f"{label} is unreadable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= maximum
    ):
        raise HostSnapshotImportError(f"{label} is not one bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HostSnapshotImportError(f"{label} could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise HostSnapshotImportError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or len(payload) != opened.st_size
        or not 1 <= len(payload) <= maximum
    ):
        raise HostSnapshotImportError(f"{label} changed or exceeded its byte bound")
    return payload


def _stage_file(target: Path, payload: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _commit_staged_file(staged: Path, target: Path) -> None:
    os.replace(staged, target)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _restore_file(target: Path, previous: bytes | None) -> None:
    if previous is None:
        target.unlink(missing_ok=True)
        return
    staged = _stage_file(target, previous)
    try:
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


@contextmanager
def _evidence_lake_transaction_lock(output: Path):
    """Serialize the local Evidence Lake comparison and two-file commit."""

    directory = output.parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        before = os.lstat(directory)
    except OSError as exc:
        raise HostSnapshotImportError(
            "evidence-lake-metrics output directory is unavailable"
        ) from exc
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise HostSnapshotImportError(
            "evidence-lake-metrics output directory must be a real directory"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise HostSnapshotImportError(
            "evidence-lake-metrics output directory could not be locked safely"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise HostSnapshotImportError(
                "evidence-lake-metrics output directory changed while opening"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = os.stat(directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise HostSnapshotImportError(
                "evidence-lake-metrics output directory changed while locking"
            )
        yield
    except OSError as exc:
        raise HostSnapshotImportError(
            "evidence-lake-metrics local transaction lock failed"
        ) from exc
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_evidence_lake_pair(
    *,
    output: Path,
    projection_raw: bytes,
    receipt_output: Path,
    receipt_raw: bytes,
    previous_projection: bytes | None,
    previous_receipt: bytes | None,
) -> None:
    if output.parent != receipt_output.parent or output == receipt_output:
        raise HostSnapshotImportError(
            "evidence-lake-metrics projection and receipt must be distinct siblings"
        )
    staged_projection = _stage_file(output, projection_raw)
    try:
        staged_receipt = _stage_file(receipt_output, receipt_raw)
    except Exception:
        staged_projection.unlink(missing_ok=True)
        raise
    committed: list[tuple[Path, bytes | None]] = []
    try:
        # Receipt first means even a process-level interruption leaves a mismatched
        # pair that a reader rejects, never an unauthenticated accepted projection.
        _commit_staged_file(staged_receipt, receipt_output)
        committed.append((receipt_output, previous_receipt))
        _commit_staged_file(staged_projection, output)
        committed.append((output, previous_projection))
        _fsync_directory(output.parent)
    except Exception as exc:
        rollback_error: Exception | None = None
        for target, previous in reversed(committed):
            try:
                _restore_file(target, previous)
            except Exception as restore_exc:  # pragma: no cover - catastrophic I/O
                rollback_error = restore_exc
        _fsync_directory(output.parent)
        if rollback_error is not None:
            raise HostSnapshotImportError(
                "evidence-lake-metrics pair commit and rollback both failed"
            ) from rollback_error
        raise HostSnapshotImportError(
            "evidence-lake-metrics projection/receipt pair commit failed"
        ) from exc
    finally:
        staged_projection.unlink(missing_ok=True)
        staged_receipt.unlink(missing_ok=True)


def _write_atomic(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    directory = target.parent
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=directory)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = ""
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _download(
    spec: HostSnapshot,
    fetcher: Fetcher,
    *,
    allow_not_found: bool,
) -> bytes | None:
    try:
        payload = fetcher(
            spec.url,
            max_bytes=spec.max_bytes,
            timeout=TIMEOUT_SECONDS,
            max_redirects=0,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
    except FetchError as exc:
        if (
            allow_not_found
            and type(exc) is FetchError
            and exc.args == ("http status 404",)
        ):
            return None
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} download failed ({type(exc).__name__})"
        ) from exc
    except (OSError, TimeoutError) as exc:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} download failed ({type(exc).__name__})"
        ) from exc
    if not isinstance(payload, bytes):
        raise HostSnapshotImportError(f"{spec.snapshot_id} fetch must return raw bytes")
    if len(payload) > spec.max_bytes:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} exceeds {spec.max_bytes} bytes"
        )
    return payload


def _compare_and_store_evidence_lake(
    spec: HostSnapshot,
    *,
    output: Path,
    key: bytes,
    checked_at: float,
    payload: bytes,
    receipt_raw: bytes,
    document: dict[str, Any],
    canonical: bytes,
    incoming_at: datetime,
    keep_last_good_on_stale: bool,
) -> ImportOutcome:
    incoming_digest = _sha256(payload)
    receipt_output = output.with_name(EVIDENCE_LAKE_RECEIPT_FILENAME)

    previous_receipt: bytes | None = None
    receipt_read_error: HostSnapshotImportError | None = None
    if receipt_output.exists() or receipt_output.is_symlink():
        try:
            previous_receipt = _read_local_file(
                receipt_output,
                maximum=EVIDENCE_LAKE_RECEIPT_MAX_BYTES,
                label="evidence-lake-metrics existing producer receipt",
            )
        except HostSnapshotImportError as exc:
            receipt_read_error = exc

    previous_payload: bytes | None = None
    if output.exists() or output.is_symlink():
        previous_payload = _read_local_file(
            output,
            maximum=spec.max_bytes,
            label="evidence-lake-metrics existing latest",
        )
        try:
            previous, previous_canonical, previous_at = _validated_payload(
                previous_payload,
                spec,
                now=checked_at,
                source="existing latest",
            )
        except HostSnapshotImportError as exc:
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} existing latest is invalid: {exc}"
            ) from exc
        previous_digest = _sha256(previous_payload)
        if incoming_at < previous_at:
            if not keep_last_good_on_stale:
                raise HostSnapshotImportError(
                    f"{spec.snapshot_id} generated_at would roll back the last-good "
                    "high-water mark (pass --keep-last-good-on-stale only in the "
                    "reviewed batch publication workflow)"
                )
            if receipt_read_error is not None:
                raise HostSnapshotImportError(
                    f"{spec.snapshot_id} cannot retain an unauthenticated local pair"
                ) from receipt_read_error
            if previous_receipt is None:
                _fail(
                    spec, "cannot retain a stale local projection without its receipt"
                )
            _validate_evidence_lake_receipt(
                previous_receipt,
                projection_raw=previous_payload,
                projection=previous,
                spec=spec,
                key=key,
                now=checked_at,
            )
            return ImportOutcome(
                snapshot_id=spec.snapshot_id,
                status="stale-kept",
                incoming_generated_at=document["generated_at"],
                retained_generated_at=previous["generated_at"],
                incoming_sha256=incoming_digest,
                retained_sha256=previous_digest,
                wrote=False,
            )
        if incoming_at == previous_at:
            if canonical != previous_canonical:
                raise HostSnapshotImportError(
                    f"{spec.snapshot_id} equivocated at generated_at "
                    f"{document['generated_at']}: equal timestamp has different content"
                )
            local_receipt_valid = False
            if receipt_read_error is None and previous_receipt is not None:
                try:
                    _validate_evidence_lake_receipt(
                        previous_receipt,
                        projection_raw=previous_payload,
                        projection=previous,
                        spec=spec,
                        key=key,
                        now=checked_at,
                    )
                    local_receipt_valid = True
                except HostSnapshotImportError:
                    local_receipt_valid = False
            if local_receipt_valid:
                return ImportOutcome(
                    snapshot_id=spec.snapshot_id,
                    status="unchanged",
                    incoming_generated_at=document["generated_at"],
                    retained_generated_at=previous["generated_at"],
                    incoming_sha256=incoming_digest,
                    retained_sha256=previous_digest,
                    wrote=False,
                )
            if previous_payload == payload:
                try:
                    _write_atomic(receipt_output, receipt_raw)
                except OSError as exc:
                    raise HostSnapshotImportError(
                        f"{spec.snapshot_id} could not repair its producer receipt"
                    ) from exc
            else:
                if receipt_read_error is not None:
                    raise HostSnapshotImportError(
                        f"{spec.snapshot_id} existing producer receipt is unsafe to replace"
                    ) from receipt_read_error
                _write_evidence_lake_pair(
                    output=output,
                    projection_raw=payload,
                    receipt_output=receipt_output,
                    receipt_raw=receipt_raw,
                    previous_projection=previous_payload,
                    previous_receipt=previous_receipt,
                )
            return ImportOutcome(
                snapshot_id=spec.snapshot_id,
                status="unchanged",
                incoming_generated_at=document["generated_at"],
                retained_generated_at=previous["generated_at"],
                incoming_sha256=incoming_digest,
                retained_sha256=incoming_digest,
                wrote=True,
            )

    if receipt_read_error is not None:
        raise HostSnapshotImportError(
            f"{spec.snapshot_id} existing producer receipt is unsafe to replace"
        ) from receipt_read_error
    _write_evidence_lake_pair(
        output=output,
        projection_raw=payload,
        receipt_output=receipt_output,
        receipt_raw=receipt_raw,
        previous_projection=previous_payload,
        previous_receipt=previous_receipt,
    )
    return ImportOutcome(
        snapshot_id=spec.snapshot_id,
        status="imported",
        incoming_generated_at=document["generated_at"],
        retained_generated_at=document["generated_at"],
        incoming_sha256=incoming_digest,
        retained_sha256=incoming_digest,
        wrote=True,
    )


def _import_evidence_lake(
    spec: HostSnapshot,
    *,
    output: Path,
    fetcher: Fetcher,
    now: float | None,
    keep_last_good_on_stale: bool,
) -> ImportOutcome:
    if spec != EVIDENCE_LAKE_SNAPSHOT:
        _fail(spec, "does not match the code-pinned Evidence Lake route")

    # The key is intentionally loaded before the first network request. A missing or
    # weak secret cannot cause even one request to the producer endpoint.
    key = _evidence_lake_key()
    checked_at = time_now(now)
    payload, receipt_raw = _download_evidence_lake_pair(spec, fetcher)
    candidate = _parse_json(payload, snapshot_id=spec.snapshot_id)
    _validate_evidence_lake_receipt(
        receipt_raw,
        projection_raw=payload,
        projection=candidate,
        spec=spec,
        key=key,
        now=checked_at,
    )
    document, canonical, incoming_at = _validated_payload(
        payload,
        spec,
        now=checked_at,
        source="incoming publication",
    )
    with _evidence_lake_transaction_lock(output):
        return _compare_and_store_evidence_lake(
            spec,
            output=output,
            key=key,
            checked_at=checked_at,
            payload=payload,
            receipt_raw=receipt_raw,
            document=document,
            canonical=canonical,
            incoming_at=incoming_at,
            keep_last_good_on_stale=keep_last_good_on_stale,
        )


def import_one(
    spec: HostSnapshot,
    *,
    output: Path,
    fetcher: Fetcher = safe_fetch_bytes,
    now: float | None = None,
    allow_empty_bootstrap_404: bool = False,
    keep_last_good_on_stale: bool = False,
) -> ImportOutcome:
    if spec.snapshot_id == EVIDENCE_LAKE_SNAPSHOT.snapshot_id:
        return _import_evidence_lake(
            spec,
            output=output,
            fetcher=fetcher,
            now=now,
            keep_last_good_on_stale=keep_last_good_on_stale,
        )
    checked_at = time_now(now)
    payload = _download(
        spec,
        fetcher,
        allow_not_found=allow_empty_bootstrap_404,
    )
    if payload is None:
        if output.exists() or output.is_symlink():
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} endpoint returned 404 after local publication began"
            )
        return ImportOutcome(
            snapshot_id=spec.snapshot_id,
            status="bootstrap-pending",
            incoming_generated_at=None,
            retained_generated_at=None,
            incoming_sha256=None,
            retained_sha256=None,
            wrote=False,
        )
    document, canonical, incoming_at = _validated_payload(
        payload,
        spec,
        now=checked_at,
        source="incoming publication",
    )
    incoming_digest = _sha256(canonical)
    if output.exists() or output.is_symlink():
        try:
            previous_payload = output.read_bytes()
        except OSError as exc:
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} existing latest is unreadable"
            ) from exc
        try:
            previous, previous_canonical, previous_at = _validated_payload(
                previous_payload,
                spec,
                now=checked_at,
                source="existing latest",
            )
        except HostSnapshotImportError as exc:
            raise HostSnapshotImportError(
                f"{spec.snapshot_id} existing latest is invalid: {exc}"
            ) from exc
        previous_digest = _sha256(previous_canonical)
        if incoming_at < previous_at:
            if not keep_last_good_on_stale:
                raise HostSnapshotImportError(
                    f"{spec.snapshot_id} generated_at would roll back the last-good "
                    "high-water mark (pass --keep-last-good-on-stale only in the "
                    "reviewed batch publication workflow)"
                )
            return ImportOutcome(
                snapshot_id=spec.snapshot_id,
                status="stale-kept",
                incoming_generated_at=document["generated_at"],
                retained_generated_at=previous["generated_at"],
                incoming_sha256=incoming_digest,
                retained_sha256=previous_digest,
                wrote=False,
            )
        if incoming_at == previous_at:
            if canonical != previous_canonical:
                raise HostSnapshotImportError(
                    f"{spec.snapshot_id} equivocated at generated_at "
                    f"{document['generated_at']}: equal timestamp has different content"
                )
            return ImportOutcome(
                snapshot_id=spec.snapshot_id,
                status="unchanged",
                incoming_generated_at=document["generated_at"],
                retained_generated_at=previous["generated_at"],
                incoming_sha256=incoming_digest,
                retained_sha256=previous_digest,
                wrote=False,
            )
    _write_atomic(output, canonical + b"\n")
    return ImportOutcome(
        snapshot_id=spec.snapshot_id,
        status="imported",
        incoming_generated_at=document["generated_at"],
        retained_generated_at=document["generated_at"],
        incoming_sha256=incoming_digest,
        retained_sha256=incoming_digest,
        wrote=True,
    )


def time_now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def import_all(
    *,
    readings: Path = READINGS,
    fetcher: Fetcher = safe_fetch_bytes,
    now: float | None = None,
    allow_empty_bootstrap_404: bool = False,
    keep_last_good_on_stale: bool = False,
) -> dict[str, ImportOutcome]:
    results: dict[str, ImportOutcome] = {}
    for spec in SNAPSHOTS:
        outcome = import_one(
            spec,
            output=Path(readings) / spec.filename,
            fetcher=fetcher,
            now=now,
            allow_empty_bootstrap_404=allow_empty_bootstrap_404,
            keep_last_good_on_stale=keep_last_good_on_stale,
        )
        results[spec.snapshot_id] = outcome
        print(json.dumps(outcome.as_dict(), sort_keys=True, separators=(",", ":")))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--readings",
        type=Path,
        default=READINGS,
        help="directory that receives the imported latest files",
    )
    parser.add_argument(
        "--keep-last-good-on-stale",
        action="store_true",
        help=(
            "when a valid host snapshot is older than the validated local high-water "
            "mark, retain the local artifact, emit a structured stale-kept outcome, "
            "and continue importing the remaining snapshots"
        ),
    )
    parser.add_argument(
        "--allow-empty-bootstrap-404",
        action="store_true",
        help=(
            "succeed without writing a missing snapshot only when that endpoint "
            "returns 404 and no local artifact exists"
        ),
    )
    args = parser.parse_args(argv)
    try:
        import_all(
            readings=args.readings,
            allow_empty_bootstrap_404=args.allow_empty_bootstrap_404,
            keep_last_good_on_stale=args.keep_last_good_on_stale,
        )
    except HostSnapshotImportError as exc:
        print(f"host snapshot import refused: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
