"""Bounded passive Cloudflare Radar TCP reset/timeout telemetry ingest.

This collector reads Cloudflare Radar's country-level distribution of where
connections terminated within their first ten client packets.  It sends one
ordinary authenticated API request for each committed country.  It does not
connect to, scan, or otherwise probe any network in those countries.

Cloudflare describes these data as a sample of TCP connections to Cloudflare
servers.  A reset or timeout can have benign causes (lost connectivity,
applications closing abruptly, or scanners), as well as attacks or third-party
interference.  The signal is therefore contextual telemetry, not proof of
censorship.  Radar API data are licensed CC BY-NC 4.0 and are attributed in
every latest reading and history row.

Only fixed country-level percentages and bounded confidence categories cross
the publication boundary.  Upstream annotation prose/URLs, packet material,
and raw connection identifiers are neither requested nor published.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from core.safe_fetch import FetchError, ResponseTooLarge, safe_fetch_bytes


UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "cloudflare_radar_tcp.json"
DEFAULT_READINGS = ROOT / "readings"
LATEST_NAME = "cloudflare-radar-tcp-latest.json"
HISTORY_NAME = "cloudflare-radar-tcp-history.jsonl"
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
TOKEN_FILE = Path("/run/secrets/cloudflare_radar_api_token")
TOKEN_MAX_BYTES = 4096

APPROVED_ENDPOINT = (
    "https://api.cloudflare.com/client/v4/radar/"
    "tcp_resets_timeouts/timeseries_groups"
)
APPROVED_HOST = "api.cloudflare.com"
APPROVED_PATH = "/client/v4/radar/tcp_resets_timeouts/timeseries_groups"
USER_AGENT = (
    "palimpsest.info Cloudflare Radar TCP passive telemetry ingest "
    "(contact desk@palimpsest.info)"
)
ATTRIBUTION = "Cloudflare Radar"
LICENSE = "CC BY-NC 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc/4.0/"
API_DOCUMENTATION_URL = (
    "https://developers.cloudflare.com/api/resources/radar/subresources/"
    "tcp_resets_timeouts/methods/timeseries_groups/"
)
METHOD_DOCUMENTATION_URL = (
    "https://developers.cloudflare.com/radar/glossary/"
    "#tcp-resets-and-timeouts"
)

METHOD_VERSION = 1
CONFIG_MAX_BYTES = 64 * 1024
LATEST_MAX_BYTES = 8 * 1024 * 1024
HISTORY_TAIL_MAX_BYTES = 256 * 1024
APPROVED_INTERVAL = "1h"
APPROVED_INTERVAL_META = "ONE_HOUR"
APPROVED_DATE_RANGE = "7d"
APPROVED_FORMAT = "JSON"
APPROVED_NORMALIZATION = "PERCENTAGE"
APPROVED_STAGES = (
    "post_syn",
    "post_ack",
    "post_psh",
    "later_in_flow",
    "no_match",
)
STAGE_DEFINITIONS = {
    "post_syn": "reset or timeout after the server received only one SYN packet",
    "post_ack": (
        "reset or timeout after the server received SYN and ACK; the connection "
        "was established"
    ),
    "post_psh": (
        "reset or timeout after the server received the first PSH data packet"
    ),
    "later_in_flow": (
        "reset within the first ten client packets after multiple data packets"
    ),
    "no_match": "all other sampled connections in the upstream distribution",
}
CONFIDENCE_LABELS = {
    0: "unspecified",
    1: "insufficient_data_and_erratic_pattern",
    2: "insufficient_data",
    3: "erratic_pattern_without_known_data_issue",
    4: "unassigned",
    5: "no_known_data_quality_issues",
}
CAUTION = (
    "TCP resets and timeouts have benign causes, including lost connectivity, "
    "applications closing abruptly, and scanning. They can also accompany attacks "
    "or third-party interference. This aggregate signal is not proof of censorship "
    "and is not causally attributable to censorship; it must be corroborated with "
    "independent evidence."
)
PRIVACY_NOTE = (
    "Publishes country-level percentage distributions and categorical confidence "
    "metadata only; no IP addresses, packet contents, connection IDs, hostnames, "
    "URLs, or other raw identifiers are requested or retained."
)

_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_CATEGORY = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_EXPECTED_TOP_LEVEL = {"schema_version", "source", "geographies", "aggregation", "limits"}
_EXPECTED_SOURCE = {
    "endpoint",
    "credential_env",
    "credential_file",
    "attribution",
    "license",
    "license_url",
    "api_documentation_url",
    "method_documentation_url",
}
_EXPECTED_AGGREGATION = {
    "interval",
    "date_range",
    "format",
    "normalization",
    "stages",
}
_EXPECTED_LIMITS = {
    "timeout_seconds",
    "retries",
    "minimum_request_interval_seconds",
    "maximum_retry_delay_seconds",
    "max_request_attempts_per_run",
    "response_bytes",
    "max_points_per_geography",
}


class RadarError(RuntimeError):
    """Base class for safe, credential-free collector failures."""


class ConfigurationError(RadarError):
    """The committed allowlist, endpoint, aggregation, or bounds are invalid."""


class CredentialError(RadarError):
    """A present credential is malformed; its value is never included here."""


class TransportError(RadarError):
    """The fixed Cloudflare API endpoint could not be read within the bounds."""


class ResponseLimitExceeded(RadarError):
    """A response or request-attempt budget exceeded a committed ceiling."""


class SchemaError(RadarError):
    """Cloudflare returned JSON outside the narrowly accepted public schema."""


class PublicationError(RadarError):
    """Existing local state is invalid or would be regressed."""


@dataclass(frozen=True)
class Limits:
    timeout_seconds: int
    retries: int
    minimum_request_interval_seconds: float
    maximum_retry_delay_seconds: float
    max_request_attempts_per_run: int
    response_bytes: int
    max_points_per_geography: int


@dataclass(frozen=True)
class RadarConfig:
    endpoint: str
    geographies: tuple[str, ...]
    interval: str
    date_range: str
    response_format: str
    normalization: str
    stages: tuple[str, ...]
    limits: Limits

    @property
    def scope_sha256(self) -> str:
        scope = {
            "endpoint": self.endpoint,
            "geographies": self.geographies,
            "interval": self.interval,
            "date_range": self.date_range,
            "format": self.response_format,
            "normalization": self.normalization,
            "stages": self.stages,
        }
        return hashlib.sha256(_canonical_json(scope)).hexdigest()


@dataclass
class RequestBudget:
    maximum: int
    consumed: int = 0

    def consume(self) -> None:
        if self.consumed >= self.maximum:
            raise ResponseLimitExceeded(
                f"request-attempt cap of {self.maximum} per run exceeded"
            )
        self.consumed += 1


@dataclass
class RequestPacer:
    minimum_interval: float
    sleeper: Callable[[float], None]
    clock: Callable[[], float]
    last_request_at: float | None = None

    def wait(self, *, additional_delay: float = 0.0) -> None:
        """Wait enough to satisfy both retry backoff and the global request rate."""

        now = self.clock()
        rate_wait = 0.0
        if self.last_request_at is not None:
            rate_wait = max(0.0, self.minimum_interval - (now - self.last_request_at))
        delay = max(rate_wait, additional_delay)
        if delay:
            self.sleeper(delay)
        self.last_request_at = self.clock()


class _BufferedResponse:
    """Small response facade preserving the injectable opener test seam."""

    status = 200

    def __init__(self, body: bytes):
        self._body = memoryview(body)
        self._offset = 0
        self.headers = {"Content-Length": str(len(body))}

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        end = min(len(self._body), self._offset + size)
        chunk = self._body[self._offset:end].tobytes()
        self._offset = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False


def _open_url(
    request: urllib.request.Request,
    *,
    timeout: float,
    max_bytes: int,
):
    """Use the repository's DNS-pinned, redirect-validating hardened GET path.

    Redirects are set to zero so the bearer header can never be forwarded, even
    to another public host.  The same committed cap is applied before buffering.
    """

    body = safe_fetch_bytes(
        request.full_url,
        max_bytes=max_bytes,
        timeout=timeout,
        max_redirects=0,
        headers=dict(request.header_items()),
    )
    return _BufferedResponse(body)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        raise ConfigurationError(f"{label} must contain exactly the documented keys")


def _integer(
    raw: Mapping[str, Any],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"limits.{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"limits.{name} must be between {minimum} and {maximum}"
        )
    return value


def _number(
    raw: Mapping[str, Any],
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"limits.{name} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ConfigurationError(
            f"limits.{name} must be between {minimum:g} and {maximum:g}"
        )
    return parsed


def load_config(path: Path | str = DEFAULT_CONFIG) -> RadarConfig:
    """Load the declarative country allowlist and reject every unsafe drift."""

    config_path = Path(path)
    try:
        if config_path.stat().st_size > CONFIG_MAX_BYTES:
            raise ConfigurationError("Cloudflare Radar config exceeds 64 KiB")
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ConfigurationError("cannot read Cloudflare Radar config") from exc
    if not isinstance(document, dict):
        raise ConfigurationError("Cloudflare Radar config must be an object")
    _require_exact_keys(document, _EXPECTED_TOP_LEVEL, "config")
    if document.get("schema_version") != 1:
        raise ConfigurationError("Cloudflare Radar config requires schema_version 1")

    source = document.get("source")
    if not isinstance(source, dict):
        raise ConfigurationError("source must be an object")
    _require_exact_keys(source, _EXPECTED_SOURCE, "source")
    endpoint = source.get("endpoint")
    if endpoint != APPROVED_ENDPOINT:
        raise ConfigurationError("source.endpoint must be the approved Cloudflare Radar API")
    parsed_endpoint = urllib.parse.urlsplit(str(endpoint))
    if (
        parsed_endpoint.scheme != "https"
        or parsed_endpoint.hostname != APPROVED_HOST
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.port not in (None, 443)
        or parsed_endpoint.path != APPROVED_PATH
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise ConfigurationError("source.endpoint must be the approved HTTPS host and path")
    expected_provenance = {
        "credential_env": TOKEN_ENV,
        "credential_file": str(TOKEN_FILE),
        "attribution": ATTRIBUTION,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "api_documentation_url": API_DOCUMENTATION_URL,
        "method_documentation_url": METHOD_DOCUMENTATION_URL,
    }
    for name, expected in expected_provenance.items():
        if source.get(name) != expected:
            raise ConfigurationError(f"source.{name} must retain official provenance")

    geography_values = document.get("geographies")
    if not isinstance(geography_values, list) or not geography_values:
        raise ConfigurationError("geographies must be a non-empty allowlist")
    geographies = tuple(geography_values)
    if len(geographies) > 16:
        raise ConfigurationError("geographies may contain at most 16 countries")
    if any(not isinstance(item, str) or not _COUNTRY.fullmatch(item) for item in geographies):
        raise ConfigurationError("geographies must be uppercase ISO alpha-2 codes")
    if len(geographies) != len(set(geographies)):
        raise ConfigurationError("geographies cannot contain duplicates")

    aggregation = document.get("aggregation")
    if not isinstance(aggregation, dict):
        raise ConfigurationError("aggregation must be an object")
    _require_exact_keys(aggregation, _EXPECTED_AGGREGATION, "aggregation")
    fixed = {
        "interval": APPROVED_INTERVAL,
        "date_range": APPROVED_DATE_RANGE,
        "format": APPROVED_FORMAT,
        "normalization": APPROVED_NORMALIZATION,
    }
    for name, expected in fixed.items():
        if aggregation.get(name) != expected:
            raise ConfigurationError(f"aggregation.{name} must remain fixed at {expected}")
    stage_values = aggregation.get("stages")
    if not isinstance(stage_values, list) or tuple(stage_values) != APPROVED_STAGES:
        raise ConfigurationError("aggregation.stages must match the fixed approved stages")

    raw_limits = document.get("limits")
    if not isinstance(raw_limits, dict):
        raise ConfigurationError("limits must be an object")
    _require_exact_keys(raw_limits, _EXPECTED_LIMITS, "limits")
    limits = Limits(
        timeout_seconds=_integer(
            raw_limits, "timeout_seconds", minimum=1, maximum=30
        ),
        retries=_integer(raw_limits, "retries", minimum=0, maximum=3),
        minimum_request_interval_seconds=_number(
            raw_limits,
            "minimum_request_interval_seconds",
            minimum=0.1,
            maximum=10.0,
        ),
        maximum_retry_delay_seconds=_number(
            raw_limits,
            "maximum_retry_delay_seconds",
            minimum=0.1,
            maximum=30.0,
        ),
        max_request_attempts_per_run=_integer(
            raw_limits,
            "max_request_attempts_per_run",
            minimum=1,
            maximum=64,
        ),
        response_bytes=_integer(
            raw_limits, "response_bytes", minimum=1024, maximum=4 * 1024 * 1024
        ),
        max_points_per_geography=_integer(
            raw_limits, "max_points_per_geography", minimum=1, maximum=1000
        ),
    )
    worst_case_attempts = len(geographies) * (limits.retries + 1)
    if limits.max_request_attempts_per_run < worst_case_attempts:
        raise ConfigurationError(
            "limits.max_request_attempts_per_run must cover the bounded retry budget"
        )
    return RadarConfig(
        endpoint=str(endpoint),
        geographies=geographies,
        interval=APPROVED_INTERVAL,
        date_range=APPROVED_DATE_RANGE,
        response_format=APPROVED_FORMAT,
        normalization=APPROVED_NORMALIZATION,
        stages=APPROVED_STAGES,
        limits=limits,
    )


def _validate_token(value: str) -> str:
    token = value.strip()
    if not token:
        raise CredentialError("Cloudflare Radar token is empty")
    if len(token) > TOKEN_MAX_BYTES or any(
        ord(char) < 0x21 or ord(char) == 0x7F for char in token
    ):
        raise CredentialError("Cloudflare Radar token has an invalid format")
    return token


def _load_token(environment: Mapping[str, str], token_file: Path) -> str | None:
    """Load a credential from an explicit CLI env or the fixed Docker secret.

    The regular environment path keeps the standalone CLI convenient. In
    production the token is mounted only into the collector worker, avoiding
    the stack-wide exposure that would result from placing it in Compose's
    shared ``.env`` file.
    """

    raw = environment.get(TOKEN_ENV, "")
    if raw.strip():
        return _validate_token(raw)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(token_file, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError("Cloudflare Radar credential file is unreadable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CredentialError("Cloudflare Radar credential file is not regular")
        if info.st_size > TOKEN_MAX_BYTES:
            raise CredentialError("Cloudflare Radar credential file exceeds its byte cap")
        payload = os.read(descriptor, TOKEN_MAX_BYTES + 1)
    except OSError as exc:
        raise CredentialError("Cloudflare Radar credential file is unreadable") from exc
    finally:
        os.close(descriptor)
    if len(payload) > TOKEN_MAX_BYTES:
        raise CredentialError("Cloudflare Radar credential file exceeds its byte cap")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise CredentialError("Cloudflare Radar credential file is not UTF-8") from exc
    if not text.strip():
        return None
    return _validate_token(text)


def _read_capped(response: Any, maximum: int) -> bytes:
    headers = getattr(response, "headers", None)
    declared = headers.get("Content-Length") if headers is not None else None
    if declared not in (None, ""):
        try:
            declared_size = int(declared)
        except (TypeError, ValueError) as exc:
            raise SchemaError("Radar response has invalid Content-Length") from exc
        if declared_size < 0:
            raise SchemaError("Radar response has negative Content-Length")
        if declared_size > maximum:
            raise ResponseLimitExceeded(
                f"Radar response exceeds the {maximum}-byte cap"
            )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, maximum - total + 1))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise SchemaError("Radar response body is not bytes")
        total += len(chunk)
        if total > maximum:
            raise ResponseLimitExceeded(
                f"Radar response exceeds the {maximum}-byte cap"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _request_url(config: RadarConfig, location: str) -> str:
    if location not in config.geographies:
        raise ConfigurationError("requested geography is outside the committed allowlist")
    query = urllib.parse.urlencode(
        (
            ("aggInterval", config.interval),
            ("dateRange", config.date_range),
            ("format", config.response_format),
            ("location", location),
        )
    )
    return f"{config.endpoint}?{query}"


def _is_retryable_http(code: int) -> bool:
    return code in {408, 425, 429} or 500 <= code <= 599


def fetch_payload(
    config: RadarConfig,
    location: str,
    token: str,
    *,
    opener: Callable[..., Any] = _open_url,
    budget: RequestBudget | None = None,
    pacer: RequestPacer | None = None,
) -> bytes:
    """Fetch one country from the sole approved endpoint with bounded retries."""

    credential = _validate_token(token)
    request = urllib.request.Request(
        _request_url(config, location),
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {credential}",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    run_budget = budget or RequestBudget(config.limits.retries + 1)
    request_pacer = pacer or RequestPacer(
        config.limits.minimum_request_interval_seconds,
        time.sleep,
        time.monotonic,
    )
    for attempt in range(config.limits.retries + 1):
        retry_delay = min(
            float(2**(attempt - 1)) if attempt else 0.0,
            config.limits.maximum_retry_delay_seconds,
        )
        request_pacer.wait(additional_delay=retry_delay)
        run_budget.consume()
        try:
            with opener(
                request,
                timeout=config.limits.timeout_seconds,
                max_bytes=config.limits.response_bytes,
            ) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = getattr(response, "code", 200)
                try:
                    status_code = int(status)
                except (TypeError, ValueError) as exc:
                    raise SchemaError("Radar response has an invalid HTTP status") from exc
                if status_code != 200:
                    if _is_retryable_http(status_code):
                        raise TransportError("Radar API returned a transient HTTP status")
                    raise SchemaError("Radar API returned a non-success HTTP status")
                return _read_capped(response, config.limits.response_bytes)
        except urllib.error.HTTPError as exc:
            retryable = _is_retryable_http(int(exc.code))
            if not retryable:
                raise TransportError(
                    f"Radar request for {location} was rejected"
                ) from None
            if attempt >= config.limits.retries:
                raise TransportError(
                    f"Radar request for {location} failed within retry bounds"
                ) from None
        except ResponseTooLarge:
            raise ResponseLimitExceeded(
                f"Radar response exceeds the {config.limits.response_bytes}-byte cap"
            ) from None
        except (ResponseLimitExceeded, SchemaError):
            raise
        except (FetchError, urllib.error.URLError, TimeoutError, OSError, TransportError):
            if attempt >= config.limits.retries:
                raise TransportError(
                    f"Radar request for {location} failed within retry bounds"
                ) from None
    raise AssertionError("unreachable")


def _parse_timestamp(value: Any, label: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or len(value) > 64:
        raise SchemaError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaError(f"{label} must include a timezone")
    parsed = parsed.astimezone(UTC)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    normalized = parsed.isoformat(timespec=timespec).replace("+00:00", "Z")
    return parsed, normalized


def _percentage(value: Any, label: str) -> int | float:
    # The official endpoint's series schema is array<string>.  Rejecting JSON
    # numbers catches an upstream contract change rather than silently coercing it.
    if not isinstance(value, str) or len(value) > 64:
        raise SchemaError(f"{label} must be a percentage string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise SchemaError(f"{label} must be a finite percentage") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise SchemaError(f"{label} must be between 0 and 100")
    rounded = parsed.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    if rounded == rounded.to_integral_value():
        return int(rounded)
    return float(rounded)


def _confidence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SchemaError("meta.confidenceInfo must be an object")
    level = raw.get("level")
    if isinstance(level, bool) or not isinstance(level, int) or level not in CONFIDENCE_LABELS:
        raise SchemaError("meta.confidenceInfo.level must be an integer from 0 through 5")
    annotations = raw.get("annotations")
    if not isinstance(annotations, list) or len(annotations) > 1000:
        raise SchemaError("meta.confidenceInfo.annotations must be a bounded array")
    sources: set[str] = set()
    event_types: set[str] = set()
    for annotation in annotations:
        if not isinstance(annotation, dict):
            raise SchemaError("each confidence annotation must be an object")
        source = annotation.get("dataSource")
        event_type = annotation.get("eventType")
        if not isinstance(source, str) or not _CATEGORY.fullmatch(source):
            raise SchemaError("confidence annotation dataSource is invalid")
        if not isinstance(event_type, str) or not _CATEGORY.fullmatch(event_type):
            raise SchemaError("confidence annotation eventType is invalid")
        sources.add(source)
        event_types.add(event_type)
    # Free-form descriptions and linked URLs are deliberately reduced to safe
    # categories so no raw or accidentally identifying material is retained.
    return {
        "level": level,
        "label": CONFIDENCE_LABELS[level],
        "annotation_count": len(annotations),
        "annotation_data_sources": sorted(sources),
        "annotation_event_types": sorted(event_types),
    }


def parse_payload(
    payload: bytes,
    *,
    location: str,
    config: RadarConfig,
) -> dict[str, Any]:
    """Strictly validate and normalize one country response."""

    try:
        document = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise SchemaError("Radar response is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or document.get("success") is not True:
        raise SchemaError("Radar response did not report success")
    if "errors" in document and document["errors"] not in ([], None):
        raise SchemaError("Radar response reported API errors")
    result = document.get("result")
    if not isinstance(result, dict) or set(result) != {"meta", "serie_0"}:
        raise SchemaError("Radar result must contain exactly meta and serie_0")
    meta = result["meta"]
    series = result["serie_0"]
    if not isinstance(meta, dict) or not isinstance(series, dict):
        raise SchemaError("Radar meta and serie_0 must be objects")
    if meta.get("aggInterval") != APPROVED_INTERVAL_META:
        raise SchemaError("Radar response aggregation interval does not match the request")
    if meta.get("normalization") != config.normalization:
        raise SchemaError("Radar response normalization is not PERCENTAGE")

    date_ranges = meta.get("dateRange")
    if not isinstance(date_ranges, list) or len(date_ranges) != 1:
        raise SchemaError("Radar meta.dateRange must contain exactly one range")
    date_range = date_ranges[0]
    if not isinstance(date_range, dict) or set(date_range) != {"startTime", "endTime"}:
        raise SchemaError("Radar date range has an invalid schema")
    start_dt, start_text = _parse_timestamp(date_range["startTime"], "dateRange.startTime")
    end_dt, end_text = _parse_timestamp(date_range["endTime"], "dateRange.endTime")
    if not start_dt < end_dt or end_dt - start_dt > timedelta(days=8):
        raise SchemaError("Radar adjusted date range is invalid or exceeds the fixed window")
    last_updated_dt, last_updated = _parse_timestamp(
        meta.get("lastUpdated"), "meta.lastUpdated"
    )
    del last_updated_dt
    confidence = _confidence(meta.get("confidenceInfo"))

    units = meta.get("units")
    if not isinstance(units, list) or not units or len(units) > 16:
        raise SchemaError("Radar meta.units must be a bounded non-empty array")
    for unit in units:
        if (
            not isinstance(unit, dict)
            or set(unit) != {"name", "value"}
            or not isinstance(unit.get("name"), str)
            or not isinstance(unit.get("value"), str)
            or len(unit["name"]) > 64
            or len(unit["value"]) > 64
        ):
            raise SchemaError("Radar unit metadata has an invalid schema")

    expected_series_keys = set(config.stages) | {"timestamps"}
    if set(series) != expected_series_keys:
        raise SchemaError("Radar series keys do not match the fixed connection stages")
    timestamps = series.get("timestamps")
    if (
        not isinstance(timestamps, list)
        or not timestamps
        or len(timestamps) > config.limits.max_points_per_geography
    ):
        raise SchemaError("Radar timestamps must be a non-empty bounded array")
    for stage in config.stages:
        values = series.get(stage)
        if not isinstance(values, list) or len(values) != len(timestamps):
            raise SchemaError(f"Radar {stage} values must align with timestamps")

    points: list[dict[str, Any]] = []
    previous: datetime | None = None
    for index, timestamp in enumerate(timestamps):
        stamp_dt, stamp_text = _parse_timestamp(timestamp, f"timestamps[{index}]")
        if previous is not None and stamp_dt <= previous:
            raise SchemaError("Radar timestamps must be unique and strictly increasing")
        if stamp_dt < start_dt or stamp_dt > end_dt:
            raise SchemaError("Radar timestamp falls outside the adjusted date range")
        previous = stamp_dt
        stage_values = {
            stage: _percentage(series[stage][index], f"{stage}[{index}]")
            for stage in config.stages
        }
        total = sum(Decimal(str(value)) for value in stage_values.values())
        if abs(total - Decimal(100)) > Decimal("0.2"):
            raise SchemaError("Radar stage percentages do not sum to approximately 100")
        points.append({"timestamp": stamp_text, "stages_pct": stage_values})

    return {
        "location": location,
        "adjusted_start": start_text,
        "adjusted_end": end_text,
        "last_updated": last_updated,
        "confidence": confidence,
        "points": points,
    }


def build_reading(
    config: RadarConfig,
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic aggregate-only snapshot from all allowlisted rows."""

    by_location = {str(item.get("location")): dict(item) for item in observations}
    if set(by_location) != set(config.geographies) or len(observations) != len(config.geographies):
        raise SchemaError("a complete reading requires exactly one row per allowlisted geography")
    ordered = [by_location[location] for location in config.geographies]
    generated_at = max(
        (str(item["last_updated"]) for item in ordered),
        key=lambda value: _parse_timestamp(value, "last_updated")[0],
    )
    base: dict[str, Any] = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "generated_at": generated_at,
        "collection_mode": "passive_upstream",
        "scope_sha256": config.scope_sha256,
        "source": {
            "name": ATTRIBUTION,
            "attribution": ATTRIBUTION,
            "endpoint": APPROVED_ENDPOINT,
            "license": LICENSE,
            "license_url": LICENSE_URL,
            "api_documentation_url": API_DOCUMENTATION_URL,
            "method_documentation_url": METHOD_DOCUMENTATION_URL,
        },
        "scope": {
            "geographies": list(config.geographies),
            "aggregation_interval": config.interval,
            "date_range": config.date_range,
            "normalization": config.normalization,
            "measurement": (
                "distribution of connection stage among sampled TCP connections "
                "terminated by reset or timeout within the first ten client packets"
            ),
        },
        "method": {
            "collection": (
                "Passive ingestion of Cloudflare Radar's aggregated API data from a "
                "sample of connections to Cloudflare servers; this collector makes no "
                "active probes."
            ),
            "stages": STAGE_DEFINITIONS,
            "confidence": (
                "Upstream Cloudflare confidence level and categorical annotation counts; "
                "annotation prose and links are not retained."
            ),
        },
        "caution": CAUTION,
        "privacy": PRIVACY_NOTE,
        "geographies": ordered,
    }
    base["snapshot_id"] = hashlib.sha256(_canonical_json(base)).hexdigest()
    return base


def _history_entry(reading: Mapping[str, Any]) -> dict[str, Any]:
    locations = []
    for geography in reading["geographies"]:
        points = geography["points"]
        if not points:
            raise PublicationError("cannot publish history without a latest point")
        locations.append(
            {
                "location": geography["location"],
                "last_updated": geography["last_updated"],
                "confidence": geography["confidence"],
                "latest_point": points[-1],
            }
        )
    return {
        "schema_version": reading["schema_version"],
        "method_version": reading["method_version"],
        "generated_at": reading["generated_at"],
        "collection_mode": reading["collection_mode"],
        "snapshot_id": reading["snapshot_id"],
        "scope_sha256": reading["scope_sha256"],
        "source": {
            "attribution": ATTRIBUTION,
            "license": LICENSE,
            "license_url": LICENSE_URL,
        },
        "aggregation_interval": reading["scope"]["aggregation_interval"],
        "date_range": reading["scope"]["date_range"],
        "caution": CAUTION,
        "geographies": locations,
    }


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _load_latest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > LATEST_MAX_BYTES:
            raise PublicationError("existing Radar latest file exceeds its local cap")
        document = json.loads(path.read_text(encoding="utf-8"))
    except PublicationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise PublicationError("existing Radar latest file is invalid") from exc
    if not isinstance(document, dict) or not isinstance(document.get("snapshot_id"), str):
        raise PublicationError("existing Radar latest file has an invalid schema")
    return document


def _last_history_snapshot(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - HISTORY_TAIL_MAX_BYTES))
        tail = handle.read(HISTORY_TAIL_MAX_BYTES)
    if size > HISTORY_TAIL_MAX_BYTES and b"\n" not in tail:
        raise PublicationError("last Radar history row exceeds its local cap")
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        document = json.loads(lines[-1].decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise PublicationError("last Radar history row is invalid") from exc
    snapshot_id = document.get("snapshot_id") if isinstance(document, dict) else None
    if not isinstance(snapshot_id, str):
        raise PublicationError("last Radar history row has an invalid schema")
    return snapshot_id


def _append_history(path: Path, entry: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _canonical_json(entry) + b"\n"
    if len(line) > HISTORY_TAIL_MAX_BYTES:
        raise PublicationError("normalized Radar history row exceeds its local cap")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        with os.fdopen(descriptor, "ab", closefd=False) as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def publish_reading(
    reading: Mapping[str, Any],
    *,
    readings: Path | str = DEFAULT_READINGS,
) -> dict[str, Any]:
    """Atomically replace latest and only append to the compact JSONL history."""

    output_dir = Path(readings)
    output_dir.mkdir(parents=True, exist_ok=True)
    latest_path = output_dir / LATEST_NAME
    history_path = output_dir / HISTORY_NAME
    lock_path = output_dir / ".cloudflare-radar-tcp.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous = _load_latest(latest_path)
        previous_id = previous.get("snapshot_id") if previous else None
        snapshot_id = reading.get("snapshot_id")
        if not isinstance(snapshot_id, str) or len(snapshot_id) != 64:
            raise PublicationError("new Radar reading has an invalid snapshot_id")
        if previous and previous_id != snapshot_id:
            previous_time, _ = _parse_timestamp(previous.get("generated_at"), "generated_at")
            current_time, _ = _parse_timestamp(reading.get("generated_at"), "generated_at")
            if current_time < previous_time:
                raise PublicationError("refusing to regress the Radar latest timestamp")

        latest_changed = previous_id != snapshot_id
        if latest_changed:
            pretty = json.dumps(
                reading,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            if len(pretty) > LATEST_MAX_BYTES:
                raise PublicationError("normalized Radar latest file exceeds its local cap")
            _atomic_write(latest_path, pretty)

        history_id = _last_history_snapshot(history_path)
        history_appended = history_id != snapshot_id
        if history_appended:
            _append_history(history_path, _history_entry(reading))
        return {
            "latest_changed": latest_changed,
            "history_appended": history_appended,
            "snapshot_id": snapshot_id,
        }


def collect_and_publish(
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    readings: Path | str = DEFAULT_READINGS,
    environ: Mapping[str, str] | None = None,
    opener: Callable[..., Any] = _open_url,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    token_file: Path = TOKEN_FILE,
) -> dict[str, Any]:
    """Collect every allowlisted country, or publish nothing on any failure.

    An unprovisioned credential gate is an intentional, successful skipped state.
    The check is performed before config or filesystem access so a gated deployment
    cannot accidentally make a network request or create a fresh-looking file.
    """

    environment = os.environ if environ is None else environ
    token = _load_token(environment, token_file)
    if token is None:
        return {
            "status": "skipped",
            "reason": "gated",
        }
    config = load_config(config_path)
    budget = RequestBudget(config.limits.max_request_attempts_per_run)
    pacer = RequestPacer(
        config.limits.minimum_request_interval_seconds,
        sleeper,
        clock,
    )
    observations = []
    for location in config.geographies:
        payload = fetch_payload(
            config,
            location,
            token,
            opener=opener,
            budget=budget,
            pacer=pacer,
        )
        observations.append(parse_payload(payload, location=location, config=config))
    reading = build_reading(config, observations)
    published = publish_reading(reading, readings=readings)
    changed = published["latest_changed"] or published["history_appended"]
    return {
        "status": "published" if changed else "unchanged",
        "snapshot_id": published["snapshot_id"],
        "geographies": len(observations),
        "points": sum(len(item["points"]) for item in observations),
        "request_attempts": budget.consumed,
        "latest_changed": published["latest_changed"],
        "history_appended": published["history_appended"],
    }
