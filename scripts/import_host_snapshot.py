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
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable

from core.safe_fetch import FetchError, safe_fetch_bytes


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
TIMEOUT_SECONDS = 15.0
EARLIEST = datetime(2025, 1, 1, tzinfo=timezone.utc)
MAX_FUTURE_SKEW_SECONDS = 300.0

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


# Deliberately not environment or CLI. Changing the trust origin is a code review.
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
)


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


SEMANTIC_VALIDATORS: dict[str, Callable[[dict[str, Any], HostSnapshot], None]] = {
    "baike-public-snapshot": _validate_baike,
    "peer-context": _validate_peer,
    "greatfire-context": _validate_greatfire,
    "public-deletion-ledgers": _validate_deletion_ledgers,
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


def import_one(
    spec: HostSnapshot,
    *,
    output: Path,
    fetcher: Fetcher = safe_fetch_bytes,
    now: float | None = None,
    allow_empty_bootstrap_404: bool = False,
    keep_last_good_on_stale: bool = False,
) -> ImportOutcome:
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
