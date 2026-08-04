"""Import the optional Palimpsest-Nemesis public snapshot.

This is the transport trust boundary between an independently deployed Nemesis
runtime and the public Palimpsest repository.  The runtime already emits an
allowlisted public document; this importer still treats every downloaded byte as
untrusted and publishes it only after transport, size, schema and time checks.

With no URL the command is deliberately a no-op.  Once
``NEMESIS_SNAPSHOT_URL`` is configured, however, an unavailable or invalid
snapshot is an error: an old committed reading may remain inspectable, but the
refresh job cannot silently describe the failed import as a successful update.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from core.safe_fetch import FetchError, safe_fetch


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "readings" / "nemesis-latest.json"
URL_ENV = "NEMESIS_SNAPSHOT_URL"
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 20.0
MAX_FUTURE_SKEW_SECONDS = 300.0
EARLIEST_TIMESTAMP = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()

SCHEMA = "palimpsest-nemesis.public-snapshot"
SCHEMA_VERSION = "1.0.0"
SOURCE = "Palimpsest-Nemesis"
STATUSES = frozenset({"ok", "degraded", "starting"})
TOP_LEVEL_FIELDS = frozenset({
    "schema", "schema_version", "source", "method", "method_version", "scope",
    "status", "methods", "generated_at", "data_timestamp", "timestamps", "health",
    "coverage", "n_alerts", "counts", "ddti", "economic", "leads", "alerts", "threats",
})
COUNT_FIELDS = frozenset({
    "posts", "sources", "topics", "economic_articles", "leads", "alerts",
    "alerts_returned", "threat_signatures",
})

Fetcher = Callable[..., str | bytes]


class SnapshotImportError(ValueError):
    """The configured snapshot could not safely cross the publication boundary."""


def validate_https_url(url: str) -> str:
    """Accept one absolute, credential-free HTTPS URL and no redirect semantics."""
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise SnapshotImportError("Nemesis snapshot URL must be a non-empty HTTPS URL")
    if any(ord(char) < 0x20 or char.isspace() for char in url):
        raise SnapshotImportError("Nemesis snapshot URL contains whitespace or control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SnapshotImportError("Nemesis snapshot URL is malformed") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SnapshotImportError("Nemesis snapshot URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise SnapshotImportError("Nemesis snapshot URL must not contain credentials")
    if parsed.fragment:
        raise SnapshotImportError("Nemesis snapshot URL must not contain a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise SnapshotImportError("Nemesis snapshot URL has an invalid port")
    return url


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotImportError(f"Nemesis snapshot repeats JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SnapshotImportError(f"Nemesis snapshot contains non-finite number {value}")


def _parse_document(payload: str | bytes) -> dict[str, Any]:
    if isinstance(payload, bytes):
        if len(payload) > MAX_BYTES:
            raise SnapshotImportError(f"Nemesis snapshot exceeds {MAX_BYTES} bytes")
        try:
            text = payload.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise SnapshotImportError("Nemesis snapshot is not valid UTF-8") from exc
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise SnapshotImportError(f"Nemesis snapshot exceeds {MAX_BYTES} bytes")
        text = payload
    else:
        raise SnapshotImportError("Nemesis snapshot fetch returned neither text nor bytes")
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except SnapshotImportError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SnapshotImportError("Nemesis snapshot is not valid bounded JSON") from exc
    if not isinstance(document, dict):
        raise SnapshotImportError("Nemesis snapshot root must be an object")
    return document


def _bounded_shape(value: Any, *, depth: int = 0) -> None:
    """Bound parser work even when a within-byte-cap document is pathologically nested."""
    if depth > 20:
        raise SnapshotImportError("Nemesis snapshot nesting exceeds 20 levels")
    if isinstance(value, dict):
        if len(value) > 500:
            raise SnapshotImportError("Nemesis snapshot object has too many fields")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 200:
                raise SnapshotImportError("Nemesis snapshot contains an invalid object key")
            _bounded_shape(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 10_000:
            raise SnapshotImportError("Nemesis snapshot array has too many entries")
        for child in value:
            _bounded_shape(child, depth=depth + 1)
    elif isinstance(value, str) and len(value) > 20_000:
        raise SnapshotImportError("Nemesis snapshot contains an oversized string")


def _timestamp(value: Any, field: str, *, now: float, required: bool) -> float | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotImportError(f"Nemesis snapshot {field} must be a Unix timestamp")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < EARLIEST_TIMESTAMP:
        raise SnapshotImportError(f"Nemesis snapshot {field} is outside the accepted time range")
    if numeric > now + MAX_FUTURE_SKEW_SECONDS:
        raise SnapshotImportError(f"Nemesis snapshot {field} is more than five minutes in the future")
    return numeric


def _nonnegative_int(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SnapshotImportError(f"Nemesis snapshot {field} must be a non-negative integer")


def validate_snapshot(document: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Validate the exact public-export identity and its health/time invariants."""
    checked_at = time.time() if now is None else float(now)
    _bounded_shape(document)

    fields = frozenset(document)
    missing = sorted(TOP_LEVEL_FIELDS - fields)
    unknown = sorted(fields - TOP_LEVEL_FIELDS)
    if missing or unknown:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise SnapshotImportError("Nemesis snapshot fields do not match schema: " + "; ".join(detail))
    if document["schema"] != SCHEMA or document["schema_version"] != SCHEMA_VERSION:
        raise SnapshotImportError("Nemesis snapshot schema identity or version is unsupported")
    if document["source"] != SOURCE:
        raise SnapshotImportError("Nemesis snapshot source identity is not Palimpsest-Nemesis")
    for field in ("method", "method_version", "scope"):
        if not isinstance(document[field], str) or not document[field].strip():
            raise SnapshotImportError(f"Nemesis snapshot {field} must be non-empty text")

    status = document["status"]
    if status not in STATUSES:
        raise SnapshotImportError("Nemesis snapshot status is unsupported")
    health = document["health"]
    if not isinstance(health, dict) or health.get("status") != status:
        raise SnapshotImportError("Nemesis snapshot health status disagrees with top-level status")
    for field in ("live", "ready", "stale"):
        if type(health.get(field)) is not bool:
            raise SnapshotImportError(f"Nemesis snapshot health.{field} must be boolean")
    if health["live"] is not True:
        raise SnapshotImportError("Nemesis snapshot exporter is not live")
    if status == "ok" and (health["ready"] is not True or health["stale"] is not False):
        raise SnapshotImportError("Nemesis snapshot status ok requires ready, non-stale health")
    if status != "ok" and health["ready"] is not False:
        raise SnapshotImportError("Nemesis snapshot non-ok status cannot claim ready health")

    generated_at = _timestamp(document["generated_at"], "generated_at", now=checked_at, required=True)
    data_timestamp = _timestamp(
        document["data_timestamp"], "data_timestamp", now=checked_at, required=status == "ok")
    if data_timestamp is not None and data_timestamp > generated_at + MAX_FUTURE_SKEW_SECONDS:
        raise SnapshotImportError("Nemesis snapshot data_timestamp is later than its export time")

    timestamps = document["timestamps"]
    if not isinstance(timestamps, dict) or "data_updated_at" not in timestamps:
        raise SnapshotImportError("Nemesis snapshot timestamps object is incomplete")
    for field, value in timestamps.items():
        _timestamp(value, f"timestamps.{field}", now=checked_at, required=False)
    freshness = health.get("freshness")
    if not isinstance(freshness, dict):
        raise SnapshotImportError("Nemesis snapshot health.freshness must be an object")
    core_data_at = _timestamp(
        freshness.get("core_data_at"), "health.freshness.core_data_at",
        now=checked_at, required=status == "ok")
    if core_data_at != data_timestamp:
        raise SnapshotImportError("Nemesis snapshot core evidence timestamps disagree")

    _nonnegative_int(document["n_alerts"], "n_alerts")
    counts = document["counts"]
    if not isinstance(counts, dict) or frozenset(counts) != COUNT_FIELDS:
        raise SnapshotImportError("Nemesis snapshot counts fields do not match schema")
    for field, value in counts.items():
        _nonnegative_int(value, f"counts.{field}")
    if counts["alerts"] != document["n_alerts"]:
        raise SnapshotImportError("Nemesis snapshot alert counts disagree")
    if counts["alerts_returned"] > counts["alerts"]:
        raise SnapshotImportError("Nemesis snapshot returns more alerts than it counts")
    for field in ("methods", "coverage"):
        if not isinstance(document[field], dict):
            raise SnapshotImportError(f"Nemesis snapshot {field} must be an object")
    for field in ("leads", "alerts"):
        if not isinstance(document[field], list):
            raise SnapshotImportError(f"Nemesis snapshot {field} must be an array")
    return document


def serialize_snapshot(document: dict[str, Any]) -> bytes:
    try:
        return (json.dumps(
            document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise SnapshotImportError("Nemesis snapshot cannot be serialized canonically") from exc


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


def import_snapshot(
    url: str,
    *,
    output: Path = DEFAULT_OUTPUT,
    fetcher: Fetcher = safe_fetch,
    now: float | None = None,
) -> dict[str, Any]:
    endpoint = validate_https_url(url)
    try:
        payload = fetcher(
            endpoint,
            max_bytes=MAX_BYTES,
            timeout=TIMEOUT_SECONDS,
            max_redirects=0,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
    except (FetchError, OSError, TimeoutError) as exc:
        # Never echo the configured URL: a deployment may use an opaque signed query.
        raise SnapshotImportError(
            f"Nemesis snapshot download failed ({type(exc).__name__})") from exc
    document = validate_snapshot(_parse_document(payload), now=now)
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
        print(f"nemesis import skipped: {URL_ENV} is unset (optional source remains absent)")
        return 0
    try:
        document = import_snapshot(args.url, output=args.output)
    except SnapshotImportError as exc:
        print(f"nemesis import failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"nemesis import -> {args.output} · status={document['status']} · "
        f"data_timestamp={document['data_timestamp']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
