"""Bounded OONI S3 bulk warehouse ingestion.

OONI publishes hourly country/test bundles in a public S3 bucket.  This module
uses unsigned ``ListObjectsV2`` requests and ordinary HTTPS GETs: no AWS SDK,
credentials, database, or analytical engine is required.  One invocation owns
exactly one UTC hour.  There is deliberately no range or backfill API here.

The private warehouse retains the upstream ``.jsonl.gz`` bytes and checksummed
manifests.  The public reading is a small aggregate containing counts by the
committed country/test allowlist.  Measurement inputs, URLs, probe identifiers,
and object keys never cross that publication boundary.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException
except ImportError:  # pragma: no cover - production dependencies require it
    ET = None
    DefusedXmlException = ValueError

from core.governance import KillSwitch


UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "ooni_bulk.json"
DEFAULT_WAREHOUSE = ROOT / "data" / "ooni-bulk"
DEFAULT_READINGS = ROOT / "readings"
LATEST_NAME = "ooni-bulk-latest.json"
HISTORY_NAME = "ooni-bulk-history.jsonl"
MANIFEST_MAX_BYTES = 16 * 1024 * 1024
_S3_XML_MAX_BYTES = 16 * 1024 * 1024
_S3_XML_MAX_ELEMENTS = 8_192
_S3_XML_MAX_DEPTH = 16
_S3_XML_MAX_TEXT_CHARS = 8_192
_S3_XML_MAX_OBJECTS = 1_000
METHOD_VERSION = 2
USER_AGENT = (
    "palimpsest.info OONI bulk warehouse "
    "(public aggregate ingest; contact desk@palimpsest.info)"
)
_APPROVED_SOURCE = (
    "ooni-data-eu-fra",
    "ooni-data-eu-fra.s3.amazonaws.com",
)
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_TEST = re.compile(r"[a-z][a-z0-9]{0,63}\Z")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\.jsonl\.gz\Z")
_TRUTHY = {"1", "true", "yes", "on"}
_FORBIDDEN_PUBLIC_KEYS = {
    "input",
    "url",
    "urls",
    "hostname",
    "probe_ip",
    "probe_id",
    "measurement_uid",
    "report_id",
}


class OONIBulkError(RuntimeError):
    """Base class for fail-loud warehouse errors."""


class ConfigurationError(OONIBulkError):
    """The committed source or limit configuration is unsafe or malformed."""


class LimitExceeded(OONIBulkError):
    """A response, object, run, quota, or free-space ceiling was reached."""


class ValidationError(OONIBulkError):
    """An S3 listing, gzip member, JSON line, or manifest was invalid."""


class TransportError(OONIBulkError):
    """The fixed public S3 endpoint could not be read successfully."""


class WarehouseBusy(OONIBulkError):
    """Another CLI or worker already owns the local warehouse lock."""


class CollectionHalted(OONIBulkError):
    """The global kill switch engaged during a resumable hour."""


@dataclass(frozen=True)
class Limits:
    listing_response_bytes: int
    listing_pages_per_scope: int
    max_objects_per_run: int
    object_bytes: int
    run_bytes: int
    uncompressed_object_bytes: int
    json_line_bytes: int
    source_quota_bytes: int
    free_space_reserve_bytes: int
    history_entries: int
    network_timeout_seconds: int
    network_retries: int


@dataclass(frozen=True)
class BulkConfig:
    bucket: str
    endpoint: str
    key_prefix: str
    countries: tuple[str, ...]
    tests: tuple[str, ...]
    lag_hours: int
    limits: Limits

    @property
    def scope_sha256(self) -> str:
        payload = {
            "bucket": self.bucket,
            "key_prefix": self.key_prefix,
            "countries": self.countries,
            "tests": self.tests,
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int
    etag: str
    last_modified: str
    country: str
    test: str

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "size": self.size,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "country": self.country,
            "test": self.test,
        }

    @classmethod
    def from_manifest_dict(cls, raw: Mapping[str, Any]) -> "S3Object":
        try:
            return cls(
                key=str(raw["key"]),
                size=int(raw["size"]),
                etag=str(raw.get("etag") or ""),
                last_modified=str(raw.get("last_modified") or ""),
                country=str(raw["country"]),
                test=str(raw["test"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid object metadata in manifest") from exc


@dataclass
class RunBudget:
    maximum: int
    consumed: int = 0

    def consume(self, amount: int) -> None:
        if amount < 0 or self.consumed + amount > self.maximum:
            raise LimitExceeded(f"run download cap of {self.maximum} bytes exceeded")
        self.consumed += amount


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """S3 should never redirect the fixed endpoint; refuse if it tries."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def _build_direct_opener():
    # urllib installs an environment-aware ProxyHandler by default. The OONI
    # lane is fixed-host direct egress, so ambient HTTPS_PROXY settings must
    # not be able to redirect private warehouse traffic through a proxy.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )


_URL_OPENER = _build_direct_opener()


def _open_url(request: urllib.request.Request, *, timeout: float):
    return _URL_OPENER.open(request, timeout=timeout)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _positive_int(raw: Mapping[str, Any], name: str, *, minimum: int = 1) -> int:
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigurationError(f"limits.{name} must be an integer")
    if value < minimum:
        raise ConfigurationError(f"limits.{name} must be at least {minimum}")
    return value


def load_config(path: Path | str = DEFAULT_CONFIG) -> BulkConfig:
    """Load and strictly validate the committed allowlist and all hard limits."""

    config_path = Path(path)
    try:
        if config_path.stat().st_size > 256 * 1024:
            raise ConfigurationError("OONI bulk config exceeds 256 KiB")
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ConfigurationError("cannot read OONI bulk config") from exc
    if (
        not isinstance(document, dict)
        or isinstance(document.get("schema_version"), bool)
        or document.get("schema_version") != 1
    ):
        raise ConfigurationError("OONI bulk config requires schema_version 1")

    source = document.get("source")
    if not isinstance(source, dict):
        raise ConfigurationError("source must be an object")
    bucket = str(source.get("bucket") or "").strip()
    endpoint = str(source.get("endpoint") or "").strip().rstrip("/")
    key_prefix = str(source.get("key_prefix") or "").strip().strip("/")
    parts = urllib.parse.urlsplit(endpoint)
    approved_bucket, approved_host = _APPROVED_SOURCE
    if (
        bucket != approved_bucket
        or parts.scheme != "https"
        or parts.hostname != approved_host
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ConfigurationError("source must be the approved public OONI S3 bucket")
    if key_prefix != "raw":
        raise ConfigurationError("source.key_prefix must be 'raw'")

    countries_raw = document.get("countries")
    tests_raw = document.get("tests")
    if not isinstance(countries_raw, list) or not countries_raw:
        raise ConfigurationError("countries must be a non-empty allowlist")
    if not isinstance(tests_raw, list) or not tests_raw:
        raise ConfigurationError("tests must be a non-empty allowlist")
    countries = tuple(str(item) for item in countries_raw)
    tests = tuple(str(item) for item in tests_raw)
    if len(countries) > 32 or any(not _COUNTRY.fullmatch(item) for item in countries):
        raise ConfigurationError("countries must contain at most 32 ISO alpha-2 codes")
    if len(tests) > 32 or any(not _TEST.fullmatch(item) for item in tests):
        raise ConfigurationError("tests must contain at most 32 archive test names")
    if len(countries) != len(set(countries)) or len(tests) != len(set(tests)):
        raise ConfigurationError("countries/tests allowlists cannot contain duplicates")
    if len(countries) * len(tests) > 256:
        raise ConfigurationError("countries/tests allowlist product cannot exceed 256")
    unsupported_tests = sorted(set(tests) - set(_NEGATIVE_CLASSIFIERS_V2))
    if unsupported_tests:
        raise ConfigurationError(
            "tests lack an explicit method-version-2 classifier: "
            + ", ".join(unsupported_tests)
        )

    lag_hours = document.get("lag_hours")
    if not isinstance(lag_hours, int) or isinstance(lag_hours, bool):
        raise ConfigurationError("lag_hours must be an integer from 1 through 48")
    if not 1 <= lag_hours <= 48:
        raise ConfigurationError("lag_hours must be an integer from 1 through 48")

    raw_limits = document.get("limits")
    if not isinstance(raw_limits, dict):
        raise ConfigurationError("limits must be an object")
    limits = Limits(
        listing_response_bytes=_positive_int(raw_limits, "listing_response_bytes", minimum=1024),
        listing_pages_per_scope=_positive_int(raw_limits, "listing_pages_per_scope"),
        max_objects_per_run=_positive_int(raw_limits, "max_objects_per_run"),
        object_bytes=_positive_int(raw_limits, "object_bytes", minimum=1024),
        run_bytes=_positive_int(raw_limits, "run_bytes", minimum=1024),
        uncompressed_object_bytes=_positive_int(
            raw_limits, "uncompressed_object_bytes", minimum=1024
        ),
        json_line_bytes=_positive_int(raw_limits, "json_line_bytes", minimum=1024),
        source_quota_bytes=_positive_int(raw_limits, "source_quota_bytes", minimum=1024),
        free_space_reserve_bytes=_positive_int(
            raw_limits, "free_space_reserve_bytes", minimum=0
        ),
        history_entries=_positive_int(raw_limits, "history_entries"),
        network_timeout_seconds=_positive_int(raw_limits, "network_timeout_seconds"),
        network_retries=_positive_int(raw_limits, "network_retries", minimum=0),
    )
    if limits.object_bytes > limits.run_bytes:
        raise ConfigurationError("limits.object_bytes cannot exceed limits.run_bytes")
    if limits.run_bytes > limits.source_quota_bytes:
        raise ConfigurationError("limits.run_bytes cannot exceed limits.source_quota_bytes")
    if limits.json_line_bytes > limits.uncompressed_object_bytes:
        raise ConfigurationError(
            "limits.json_line_bytes cannot exceed limits.uncompressed_object_bytes"
        )
    return BulkConfig(
        bucket=bucket,
        endpoint=endpoint,
        key_prefix=key_prefix,
        countries=countries,
        tests=tests,
        lag_hours=lag_hours,
        limits=limits,
    )


def parse_hour(value: str | datetime) -> datetime:
    """Return an exact UTC hour from a small set of explicit CLI-safe forms."""

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        formats = (
            "%Y-%m-%dT%H",
            "%Y-%m-%dT%H:00Z",
            "%Y-%m-%dT%H:00:00Z",
            "%Y%m%d%H",
        )
        parsed = None
        for fmt in formats:
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError("hour must be YYYY-MM-DDTHH (UTC)")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("hour must identify an exact UTC hour")
    return parsed


def latest_lagged_hour(
    config: BulkConfig,
    *,
    now: datetime | None = None,
) -> datetime:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    head = observed.replace(minute=0, second=0, microsecond=0)
    return head - timedelta(hours=config.lag_hours)


def format_hour(hour: datetime) -> str:
    return hour.astimezone(UTC).strftime("%Y-%m-%dT%H:00:00Z")


def _hour_prefix(config: BulkConfig, hour: datetime) -> str:
    return f"{config.key_prefix}/{hour:%Y%m%d}/{hour:%H}/"


def _scope_prefix(config: BulkConfig, hour: datetime, country: str, test: str) -> str:
    return f"{_hour_prefix(config, hour)}{country}/{test}/"


def _xml_child_text(parent, name: str) -> str | None:
    child = parent.find(f"{{*}}{name}")
    return child.text if child is not None else None


def _validate_listing_tree(root) -> None:
    seen = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        seen += 1
        if seen > _S3_XML_MAX_ELEMENTS or depth > _S3_XML_MAX_DEPTH:
            raise ValidationError("S3 listing XML exceeded structural limits")
        if len(element.text or "") > _S3_XML_MAX_TEXT_CHARS:
            raise ValidationError("S3 listing XML field exceeded its size limit")
        stack.extend((child, depth + 1) for child in element)


def parse_list_objects_v2(
    payload: bytes,
    *,
    expected_bucket: str,
    expected_prefix: str,
    country: str,
    test: str,
) -> tuple[list[S3Object], str | None]:
    """Parse one bounded ListObjectsV2 page and reject scope confusion."""

    if ET is None:
        raise ValidationError("hardened XML parser is unavailable")
    if not isinstance(payload, bytes) or len(payload) > _S3_XML_MAX_BYTES:
        raise ValidationError("S3 listing XML exceeded its size limit")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, DefusedXmlException, ValueError, TypeError) as exc:
        raise ValidationError("invalid S3 ListObjectsV2 XML") from exc
    _validate_listing_tree(root)
    if root.tag.rsplit("}", 1)[-1] != "ListBucketResult":
        raise ValidationError("unexpected S3 listing root element")
    if _xml_child_text(root, "Name") != expected_bucket:
        raise ValidationError("S3 listing named an unexpected bucket")
    if _xml_child_text(root, "Prefix") != expected_prefix:
        raise ValidationError("S3 listing echoed an unexpected prefix")

    objects: list[S3Object] = []
    seen: set[str] = set()
    for content in root.findall("{*}Contents"):
        if len(objects) >= _S3_XML_MAX_OBJECTS:
            raise ValidationError("S3 listing returned more than 1000 objects")
        key = _xml_child_text(content, "Key") or ""
        if not key.startswith(expected_prefix):
            raise ValidationError("S3 listing returned an object outside the requested scope")
        remainder = key[len(expected_prefix):]
        # OONI currently publishes a duplicate tar bundle beside each JSONL
        # bundle.  The suffix test intentionally excludes that second copy.
        if not remainder.endswith(".jsonl.gz"):
            continue
        if "/" in remainder or not _FILENAME.fullmatch(remainder):
            raise ValidationError("S3 listing returned an unsafe object name")
        try:
            size = int(_xml_child_text(content, "Size") or "")
        except ValueError as exc:
            raise ValidationError("S3 listing returned an invalid object size") from exc
        if size < 0:
            raise ValidationError("S3 listing returned a negative object size")
        if key in seen:
            raise ValidationError("S3 listing repeated an object key")
        seen.add(key)
        objects.append(S3Object(
            key=key,
            size=size,
            etag=(_xml_child_text(content, "ETag") or "").strip('"'),
            last_modified=_xml_child_text(content, "LastModified") or "",
            country=country,
            test=test,
        ))

    truncated = (_xml_child_text(root, "IsTruncated") or "false").strip().lower()
    token = _xml_child_text(root, "NextContinuationToken")
    if truncated == "true" and not token:
        raise ValidationError("truncated S3 listing omitted its continuation token")
    if truncated not in {"true", "false"}:
        raise ValidationError("S3 listing returned invalid IsTruncated")
    return objects, token if truncated == "true" else None


def _read_capped(response: Any, maximum: int) -> bytes:
    header = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        header = headers.get("Content-Length")
    if header:
        try:
            declared = int(header)
        except ValueError as exc:
            raise ValidationError("response carried an invalid Content-Length") from exc
        if declared > maximum:
            raise LimitExceeded(f"response exceeds {maximum} bytes")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, maximum - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise LimitExceeded(f"response exceeds {maximum} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _request_listing_page(
    config: BulkConfig,
    *,
    prefix: str,
    continuation_token: str | None,
    opener: Callable[..., Any],
    halt_check: Callable[[], bool] | None = None,
) -> bytes:
    query = {
        "list-type": "2",
        "prefix": prefix,
        "max-keys": "1000",
    }
    if continuation_token:
        query["continuation-token"] = continuation_token
    url = f"{config.endpoint}/?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/xml",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    attempts = config.limits.network_retries + 1
    for attempt in range(attempts):
        if halt_check is not None and halt_check():
            raise CollectionHalted("global kill switch is engaged")
        try:
            with opener(request, timeout=config.limits.network_timeout_seconds) as response:
                status = getattr(response, "status", None) or getattr(response, "code", 200)
                if int(status) != 200:
                    raise TransportError(f"S3 listing returned HTTP {status}")
                return _read_capped(response, config.limits.listing_response_bytes)
        except (LimitExceeded, ValidationError):
            raise
        except (urllib.error.URLError, OSError, TransportError) as exc:
            if attempt + 1 >= attempts:
                raise TransportError("S3 listing request failed") from exc
            time.sleep(min(2 ** attempt, 8))
    raise AssertionError("unreachable")


def list_scope_objects(
    config: BulkConfig,
    hour: datetime,
    country: str,
    test: str,
    *,
    opener: Callable[..., Any] = _open_url,
    halt_check: Callable[[], bool] | None = None,
) -> list[S3Object]:
    """List one exact hourly country/test prefix with bounded pagination."""

    if country not in config.countries or test not in config.tests:
        raise ConfigurationError("country/test is outside the committed allowlist")
    prefix = _scope_prefix(config, hour, country, test)
    objects: list[S3Object] = []
    seen: set[str] = set()
    token = None
    for _page in range(config.limits.listing_pages_per_scope):
        payload = _request_listing_page(
            config,
            prefix=prefix,
            continuation_token=token,
            opener=opener,
            halt_check=halt_check,
        )
        page, token = parse_list_objects_v2(
            payload,
            expected_bucket=config.bucket,
            expected_prefix=prefix,
            country=country,
            test=test,
        )
        for item in page:
            if item.key in seen:
                raise ValidationError("paginated S3 listing repeated an object key")
            seen.add(item.key)
            objects.append(item)
        if token is None:
            return objects
    raise LimitExceeded(
        f"S3 listing exceeded {config.limits.listing_pages_per_scope} pages for one scope"
    )


def _normalised_test_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _signal_is_positive(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "none", "null", "ok"}
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return bool(value)


def _negative_webconnectivity(keys: Mapping[str, Any]) -> bool:
    return keys.get("accessible") is False or keys.get("blocking") in {
        "dns",
        "http-diff",
        "http-failure",
        "tcp_ip",
    }


def _negative_telegram(keys: Mapping[str, Any]) -> bool:
    return (
        keys.get("telegram_http_blocking") is True
        or keys.get("telegram_tcp_blocking") is True
        or keys.get("telegram_web_status") == "blocked"
    )


def _negative_whatsapp(keys: Mapping[str, Any]) -> bool:
    if any(
        keys.get(name) == "blocked"
        for name in (
            "registration_server_status",
            "whatsapp_endpoints_status",
            "whatsapp_web_status",
        )
    ):
        return True
    # Retain the explicit legacy endpoint result as a fallback for older rows
    # whose summary status is absent. Empty and malformed values are not
    # evidence of a negative outcome.
    blocked = keys.get("whatsapp_endpoints_blocked")
    return isinstance(blocked, list) and bool(blocked)


def _negative_signal(keys: Mapping[str, Any]) -> bool:
    return keys.get("signal_backend_status") == "blocked"


_TOR_TARGET_CLASSES = (
    "dir_port",
    "obfs4",
    "or_port_dirauth",
    "or_port",
)


def _negative_tor(keys: Mapping[str, Any]) -> bool:
    for target_class in _TOR_TARGET_CLASSES:
        total = keys.get(f"{target_class}_total")
        accessible = keys.get(f"{target_class}_accessible")
        if (
            isinstance(total, int)
            and not isinstance(total, bool)
            and isinstance(accessible, int)
            and not isinstance(accessible, bool)
            and total > 0
            and 0 <= accessible < total
        ):
            return True
    return False


def _negative_psiphon(keys: Mapping[str, Any]) -> bool:
    failure = keys.get("failure")
    return isinstance(failure, str) and bool(failure.strip())


def _negative_riseupvpn(keys: Mapping[str, Any]) -> bool:
    failures = keys.get("api_failures")
    return keys.get("ca_cert_status") is False or (
        isinstance(failures, list) and bool(failures)
    )


# METHOD_VERSION 2 deliberately has no generic suffix/name heuristic. Each
# allowlisted OONI test is tied to fields whose outcome semantics are specified
# by that nettest. Adding a test therefore requires an explicit method change.
_NEGATIVE_CLASSIFIERS_V2: Mapping[str, Callable[[Mapping[str, Any]], bool]] = {
    "webconnectivity": _negative_webconnectivity,
    "telegram": _negative_telegram,
    "whatsapp": _negative_whatsapp,
    "signal": _negative_signal,
    "tor": _negative_tor,
    "psiphon": _negative_psiphon,
    "riseupvpn": _negative_riseupvpn,
}


def _measurement_counters(
    document: Mapping[str, Any],
    *,
    test: str,
) -> tuple[int, int]:
    keys = document.get("test_keys")
    if not isinstance(keys, Mapping):
        keys = {}
    failure = _signal_is_positive(document.get("failure"))
    for name, value in keys.items():
        lowered = str(name).lower()
        if lowered == "failed_operation" or lowered.endswith("_failure") or lowered == "failure":
            failure = failure or _signal_is_positive(value)

    classifier = _NEGATIVE_CLASSIFIERS_V2.get(test)
    if classifier is None:
        raise ValidationError(f"no method-version-2 classifier for test {test!r}")
    return int(failure), int(classifier(keys))


def validate_jsonl_gzip(
    path: Path | str,
    *,
    country: str,
    test: str,
    uncompressed_maximum: int,
    line_maximum: int,
) -> dict[str, int]:
    """Stream-validate gzip/UTF-8/JSONL and return only privacy-safe counters."""

    records = failures = negatives = uncompressed = 0
    try:
        with gzip.open(path, "rb") as handle:
            while True:
                line = handle.readline(line_maximum + 1)
                if not line:
                    break
                uncompressed += len(line)
                if len(line) > line_maximum:
                    raise LimitExceeded(f"JSON line exceeds {line_maximum} bytes")
                if uncompressed > uncompressed_maximum:
                    raise LimitExceeded(
                        f"gzip output exceeds {uncompressed_maximum} bytes"
                    )
                body = line.rstrip(b"\r\n")
                if not body:
                    raise ValidationError("OONI JSONL contains an empty line")
                try:
                    document = json.loads(body.decode("utf-8", "strict"))
                except (UnicodeDecodeError, ValueError, TypeError) as exc:
                    raise ValidationError("OONI object contains invalid UTF-8 JSONL") from exc
                if not isinstance(document, dict):
                    raise ValidationError("OONI JSONL record must be an object")
                if document.get("probe_cc") != country:
                    raise ValidationError("OONI record country does not match object scope")
                if _normalised_test_name(document.get("test_name")) != test:
                    raise ValidationError("OONI record test does not match object scope")
                failed, negative = _measurement_counters(document, test=test)
                records += 1
                failures += failed
                negatives += negative
    except LimitExceeded:
        raise
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise ValidationError("OONI object is not a complete valid gzip stream") from exc
    if records == 0:
        raise ValidationError("OONI object contains no JSONL records")
    return {
        "measurements": records,
        "failed_measurements": failures,
        "negative_measurements": negatives,
        "uncompressed_bytes": uncompressed,
    }


def _safe_destination(warehouse: Path, item: S3Object) -> Path:
    parts = item.key.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValidationError("unsafe S3 object key")
    if not _FILENAME.fullmatch(parts[-1]):
        raise ValidationError("unsafe S3 object filename")
    return warehouse / "objects" / Path(*parts)


def _sha256_file(path: Path, *, expected_size: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    if expected_size is not None and total != expected_size:
        raise ValidationError("warehouse object size does not match its manifest")
    return digest.hexdigest()


def _download_one_attempt(
    config: BulkConfig,
    item: S3Object,
    destination: Path,
    *,
    opener: Callable[..., Any],
    budget: RunBudget,
    halt_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".partial-",
        suffix=".jsonl.gz",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    handle = os.fdopen(fd, "wb")
    try:
        if halt_check is not None and halt_check():
            raise CollectionHalted("global kill switch is engaged")
        url = f"{config.endpoint}/{urllib.parse.quote(item.key, safe='/')}"
        request_headers = {
            "Accept": "application/gzip, application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        if item.etag:
            # Bind the GET to the version returned by ListObjectsV2. A late
            # overwrite cannot silently pair old metadata with new bytes.
            request_headers["If-Match"] = f'"{item.etag}"'
        request = urllib.request.Request(url, headers=request_headers)
        with opener(request, timeout=config.limits.network_timeout_seconds) as response:
            status = getattr(response, "status", None) or getattr(response, "code", 200)
            if int(status) != 200:
                raise TransportError(f"S3 object request returned HTTP {status}")
            headers = getattr(response, "headers", None)
            declared = headers.get("Content-Length") if headers is not None else None
            response_etag = headers.get("ETag") if headers is not None else None
            if item.etag and response_etag and response_etag.strip('"') != item.etag:
                raise ValidationError("object ETag differs from S3 listing")
            if declared:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    raise ValidationError("object response has invalid Content-Length") from exc
                if declared_size != item.size:
                    raise ValidationError("object Content-Length differs from S3 listing")
            digest = hashlib.sha256()
            total = 0
            while True:
                if halt_check is not None and halt_check():
                    raise CollectionHalted("global kill switch is engaged")
                # Read at most one byte beyond the size admitted from the S3
                # listing. This catches a headerless oversized response before
                # unlisted bytes can be written into reserved warehouse space.
                chunk = response.read(min(1024 * 1024, item.size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > item.size:
                    raise ValidationError(
                        "object response exceeds the size in the S3 listing"
                    )
                budget.consume(len(chunk))
                if total > config.limits.object_bytes:
                    raise LimitExceeded(
                        f"object download exceeds {config.limits.object_bytes} bytes"
                    )
                handle.write(chunk)
                digest.update(chunk)
        if total != item.size:
            raise ValidationError("downloaded object size differs from S3 listing")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        counters = validate_jsonl_gzip(
            temporary,
            country=item.country,
            test=item.test,
            uncompressed_maximum=config.limits.uncompressed_object_bytes,
            line_maximum=config.limits.json_line_bytes,
        )
        # Raw OONI rows can contain probe metadata and measurement inputs. The
        # public aggregate is world-readable; these private source bytes are not.
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        return {
            **item.as_manifest_dict(),
            "sha256": digest.hexdigest(),
            **counters,
        }
    finally:
        if not handle.closed:
            handle.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def download_object(
    config: BulkConfig,
    item: S3Object,
    destination: Path,
    *,
    opener: Callable[..., Any] = _open_url,
    budget: RunBudget | None = None,
    halt_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Stream one object to a validated temporary file, then atomically commit."""

    if item.size > config.limits.object_bytes:
        raise LimitExceeded(
            f"listed object is {item.size} bytes; cap is {config.limits.object_bytes}"
        )
    run_budget = budget or RunBudget(config.limits.run_bytes)
    attempts = config.limits.network_retries + 1
    for attempt in range(attempts):
        try:
            return _download_one_attempt(
                config,
                item,
                destination,
                opener=opener,
                budget=run_budget,
                halt_check=halt_check,
            )
        except (LimitExceeded, ValidationError):
            raise
        except (urllib.error.URLError, OSError, TransportError) as exc:
            if attempt + 1 >= attempts:
                raise TransportError("S3 object download failed") from exc
            time.sleep(min(2 ** attempt, 8))
    raise AssertionError("unreachable")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
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
            # Directory fsync is not available on every test/platform FS.  The
            # file itself is still synced and atomically renamed.
            pass
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _manifest_path(warehouse: Path, hour: datetime) -> Path:
    return warehouse / "manifests" / f"{hour:%Y}" / f"{hour:%m}" / f"{hour:%d}" / f"{hour:%H}.json"


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    body = _canonical_json(manifest)
    envelope = {
        "checksum": hashlib.sha256(body).hexdigest(),
        "manifest": manifest,
    }
    _atomic_write(path, json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n")


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > MANIFEST_MAX_BYTES:
            raise ValidationError("warehouse manifest exceeds 16 MiB")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        manifest = envelope["manifest"]
        checksum = envelope["checksum"]
    except ValidationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise ValidationError("warehouse manifest is invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(checksum, str):
        raise ValidationError("warehouse manifest envelope is invalid")
    if hashlib.sha256(_canonical_json(manifest)).hexdigest() != checksum:
        raise ValidationError("warehouse manifest checksum mismatch")
    return manifest


def _new_manifest(config: BulkConfig, hour: datetime, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "hour": format_hour(hour),
        "hour_prefix": _hour_prefix(config, hour),
        "scope_sha256": config.scope_sha256,
        "state": "partial",
        "started_at": now.isoformat().replace("+00:00", "Z"),
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "scopes": {},
        "objects": {},
    }


def _entry_matches(item: S3Object, entry: Mapping[str, Any]) -> bool:
    try:
        return (
            int(entry.get("size")) == item.size
            and str(entry.get("etag") or "") == item.etag
            and str(entry.get("country") or "") == item.country
            and str(entry.get("test") or "") == item.test
            and isinstance(entry.get("sha256"), str)
            and len(str(entry["sha256"])) == 64
            and int(entry.get("measurements")) > 0
        )
    except (TypeError, ValueError):
        return False


def _reuse_entry(
    config: BulkConfig,
    warehouse: Path,
    item: S3Object,
    entry: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    destination = _safe_destination(warehouse, item)
    if entry is not None and _entry_matches(item, entry) and destination.is_file():
        try:
            digest = _sha256_file(destination, expected_size=item.size)
        except (OSError, ValidationError):
            return None
        if digest == entry.get("sha256"):
            return dict(entry)
        return None
    if not destination.is_file() or destination.stat().st_size != item.size:
        return None
    # Recover the narrow crash window after object rename but before manifest
    # commit: validate the orphaned final file and adopt it without egress.
    try:
        digest = _sha256_file(destination, expected_size=item.size)
        counters = validate_jsonl_gzip(
            destination,
            country=item.country,
            test=item.test,
            uncompressed_maximum=config.limits.uncompressed_object_bytes,
            line_maximum=config.limits.json_line_bytes,
        )
    except (OSError, OONIBulkError):
        return None
    return {
        **item.as_manifest_dict(),
        "sha256": digest,
        **counters,
    }


def _directory_size(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for directory, _subdirs, filenames in os.walk(root):
        for filename in filenames:
            try:
                total += (Path(directory) / filename).stat().st_size
            except FileNotFoundError:
                continue
    return total


def _remove_stale_partials(warehouse: Path) -> None:
    objects = warehouse / "objects"
    if not objects.exists():
        return
    for path in objects.rglob(".partial-*"):
        if path.is_file():
            try:
                path.unlink()
            except FileNotFoundError:
                pass


class _WarehouseLock:
    def __init__(self, warehouse: Path):
        self.path = warehouse / ".ingest.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise WarehouseBusy("another OONI warehouse run is active") from exc
        return self

    def __exit__(self, *_args):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _objects_from_manifest_scopes(manifest: Mapping[str, Any]) -> list[S3Object]:
    out: list[S3Object] = []
    scopes = manifest.get("scopes")
    if not isinstance(scopes, Mapping):
        raise ValidationError("manifest scopes must be an object")
    for raw_scope in scopes.values():
        if not isinstance(raw_scope, Mapping) or raw_scope.get("listed") is not True:
            continue
        listed = raw_scope.get("objects")
        if not isinstance(listed, list):
            raise ValidationError("manifest scope objects must be a list")
        out.extend(S3Object.from_manifest_dict(item) for item in listed)
    keys = [item.key for item in out]
    if len(keys) != len(set(keys)):
        raise ValidationError("manifest scopes repeat an object key")
    return out


def _build_rollup(
    config: BulkConfig,
    hour: datetime,
    entries: Mapping[str, Mapping[str, Any]],
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    totals = {
        "objects": 0,
        "compressed_bytes": 0,
        "uncompressed_bytes": 0,
        "measurements": 0,
        "failed_measurements": 0,
        "negative_measurements": 0,
    }
    for country in config.countries:
        for test in config.tests:
            cell_entries = [
                entry for entry in entries.values()
                if entry.get("country") == country and entry.get("test") == test
            ]
            cell = {
                "country": country,
                "test": test,
                "objects": len(cell_entries),
                "compressed_bytes": sum(int(item.get("size", 0)) for item in cell_entries),
                "uncompressed_bytes": sum(
                    int(item.get("uncompressed_bytes", 0)) for item in cell_entries
                ),
                "measurements": sum(
                    int(item.get("measurements", 0)) for item in cell_entries
                ),
                "failed_measurements": sum(
                    int(item.get("failed_measurements", 0)) for item in cell_entries
                ),
                "negative_measurements": sum(
                    int(item.get("negative_measurements", 0)) for item in cell_entries
                ),
            }
            cells.append(cell)
            for field in totals:
                totals[field] += int(cell[field])
    rollup = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "hour": format_hour(hour),
        "window_end": format_hour(hour + timedelta(hours=1)),
        "source": "OONI public S3 bulk archive",
        "method": (
            "unsigned hourly S3 listing; allowlisted JSONL gzip objects are retained "
            "privately and streamed into aggregate counters using explicit per-test "
            "outcome classifiers"
        ),
        "privacy": (
            "aggregate counts only; measurement inputs, addresses, probe identifiers, "
            "object keys, and request destinations are not published"
        ),
        "counter_definitions": {
            "measurements": "valid JSONL measurement records in retained objects",
            "failed_measurements": (
                "records carrying a non-empty generic or test-specific failure field"
            ),
            "negative_measurements": (
                "records with an explicit per-test negative outcome: Web Connectivity "
                "is inaccessible or blocked; Telegram, WhatsApp, or Signal reports a "
                "blocked component; Tor has fewer accessible than total targets in a "
                "non-empty target class; Psiphon reports a failure; or RiseupVPN reports "
                "an API/certificate failure; this is not OONI's official anomaly "
                "classification"
            ),
        },
        "scope_sha256": config.scope_sha256,
        "history_retention_hours": config.limits.history_entries,
        "countries": list(config.countries),
        "tests": list(config.tests),
        **totals,
        "cells": cells,
    }
    _assert_public_privacy(rollup)
    return rollup


def _assert_public_privacy(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_PUBLIC_KEYS:
                raise ValidationError("public OONI rollup contains a forbidden field")
            _assert_public_privacy(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_public_privacy(item)
    elif isinstance(value, str) and "://" in value:
        raise ValidationError("public OONI rollup contains a network location")


def _read_history(path: Path, *, maximum_entries: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    # A valid history is bounded on every write.  Refuse a legacy/corrupt file
    # that is far beyond that contract instead of reading it into memory.
    maximum_bytes = max(4 * 1024 * 1024, maximum_entries * 128 * 1024)
    if path.stat().st_size > maximum_bytes:
        raise LimitExceeded("OONI bulk history exceeds its bounded read ceiling")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("hour"), str):
                raise ValidationError("OONI bulk history contains an invalid row")
            _assert_public_privacy(row)
            rows.append(row)
    # A crash cannot grow the file because writes are atomic, but this slice
    # also protects an operator-edited file before adding the next row.
    return rows[-maximum_entries:]


def publish_rollup(
    rollup: Mapping[str, Any],
    *,
    readings: Path | str = DEFAULT_READINGS,
    history_entries: int,
) -> dict[str, bool]:
    """Atomically upsert one bounded history hour without regressing latest."""

    _assert_public_privacy(rollup)
    readings_root = Path(readings)
    latest_path = readings_root / LATEST_NAME
    history_path = readings_root / HISTORY_NAME
    rows = _read_history(history_path, maximum_entries=history_entries)
    rows = [row for row in rows if row.get("hour") != rollup.get("hour")]
    rows.append(dict(rollup))
    rows.sort(key=lambda row: str(row["hour"]))
    rows = rows[-history_entries:]
    history_payload = b"".join(_canonical_json(row) + b"\n" for row in rows)
    _atomic_write(history_path, history_payload)

    latest_updated = True
    if latest_path.exists():
        try:
            current = json.loads(latest_path.read_text(encoding="utf-8"))
            current_hour = parse_hour(str(current.get("hour")))
            candidate_hour = parse_hour(str(rollup.get("hour")))
            latest_updated = candidate_hour >= current_hour
        except (OSError, UnicodeError, ValueError, TypeError):
            latest_updated = True
    if latest_updated:
        latest_payload = json.dumps(
            rollup, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8") + b"\n"
        _atomic_write(latest_path, latest_payload)
    return {"history_updated": True, "latest_updated": latest_updated}


def _result(
    rollup: Mapping[str, Any],
    *,
    started: float,
    budget: RunBudget,
    downloaded: int,
    reused: int,
    idempotent: bool,
) -> dict[str, Any]:
    return {
        "collector": "ooni-bulk",
        "status": "success",
        "records_collected": int(rollup.get("measurements", 0)),
        "duration_seconds": round(time.monotonic() - started, 2),
        "generated_at": rollup.get("generated_at"),
        "hour": rollup.get("hour"),
        "objects": int(rollup.get("objects", 0)),
        "objects_downloaded": downloaded,
        "objects_reused": reused,
        "bytes_downloaded": budget.consumed,
        "idempotent": idempotent,
        "error": "",
    }


def ingest_hour(
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    hour: str | datetime | None = None,
    warehouse: Path | str | None = None,
    readings: Path | str = DEFAULT_READINGS,
    now: datetime | None = None,
    opener: Callable[..., Any] = _open_url,
    kill_switch: KillSwitch | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    usage_provider: Callable[[Path], int] = _directory_size,
) -> dict[str, Any]:
    """Ingest exactly one hour, resuming only that hour from its manifest."""

    started = time.monotonic()
    config = load_config(config_path)
    observed_now = (now or datetime.now(UTC)).astimezone(UTC)
    target = parse_hour(hour) if hour is not None else latest_lagged_hour(config, now=observed_now)
    if target >= observed_now.replace(minute=0, second=0, microsecond=0):
        raise ValueError("hour must be in the past")
    warehouse_root = Path(
        warehouse
        or os.getenv("PALIMPSEST_OONI_WAREHOUSE_DIR", "").strip()
        or DEFAULT_WAREHOUSE
    )
    resolved_warehouse = warehouse_root.resolve(strict=False)
    resolved_readings = Path(readings).resolve(strict=False)
    if resolved_warehouse == Path(resolved_warehouse.anchor):
        raise ConfigurationError("warehouse root cannot be a filesystem root")
    if (
        resolved_warehouse == resolved_readings
        or resolved_readings in resolved_warehouse.parents
    ):
        raise ConfigurationError("private warehouse cannot live inside public readings")
    kill = kill_switch or KillSwitch()
    if kill.is_halted():
        return {
            "collector": "ooni-bulk",
            "status": "halted",
            "records_collected": 0,
            "duration_seconds": 0.0,
            "hour": format_hour(target),
            "error": "global kill switch is engaged",
        }

    warehouse_root.mkdir(parents=True, exist_ok=True)
    with _WarehouseLock(warehouse_root):
        # A crash may leave only disposable same-directory temporary files.
        # Remove those before calculating quota so they cannot strand the lane.
        _remove_stale_partials(warehouse_root)
        # Listings/manifests are small compared with raw objects, but they are
        # still writes. Preserve enough headroom for one maximum manifest and
        # its atomic temporary copy before making the first network request.
        initial_free = int(disk_usage(warehouse_root).free)
        if initial_free - (2 * MANIFEST_MAX_BYTES) < config.limits.free_space_reserve_bytes:
            raise LimitExceeded("free-space reserve leaves no manifest working room")
        if usage_provider(warehouse_root) >= config.limits.source_quota_bytes:
            raise LimitExceeded("OONI warehouse is already at its source quota")
        manifest_path = _manifest_path(warehouse_root, target)
        manifest = _load_manifest(manifest_path)
        if manifest is None:
            manifest = _new_manifest(config, target, observed_now)
        elif manifest.get("schema_version") != 1:
            raise ValidationError("unsupported warehouse manifest schema")
        elif manifest.get("hour") != format_hour(target):
            raise ValidationError("warehouse manifest belongs to a different hour")
        if manifest.get("method_version") != METHOD_VERSION:
            # Keep downloaded source files and listed scope metadata, but
            # re-validate/re-aggregate every object under the new method.
            manifest.update({
                "method_version": METHOD_VERSION,
                "state": "partial",
                "objects": {},
                "updated_at": observed_now.isoformat().replace("+00:00", "Z"),
            })
            manifest.pop("rollup", None)
            manifest.pop("completed_at", None)
            _write_manifest(manifest_path, manifest)
        if manifest.get("scope_sha256") != config.scope_sha256:
            manifest.update({
                "scope_sha256": config.scope_sha256,
                "state": "partial",
                "scopes": {},
                "updated_at": observed_now.isoformat().replace("+00:00", "Z"),
            })
            manifest.pop("rollup", None)
            manifest.pop("completed_at", None)
            _write_manifest(manifest_path, manifest)

        budget = RunBudget(config.limits.run_bytes)
        objects_map = manifest.setdefault("objects", {})
        if not isinstance(objects_map, dict):
            raise ValidationError("manifest objects must be an object")

        if manifest.get("state") == "complete" and isinstance(manifest.get("rollup"), dict):
            listed = _objects_from_manifest_scopes(manifest)
            all_reusable = all(
                _reuse_entry(config, warehouse_root, item, objects_map.get(item.key))
                is not None
                for item in listed
            )
            if all_reusable:
                rollup = manifest["rollup"]
                _assert_public_privacy(rollup)
                publish_rollup(
                    rollup,
                    readings=readings,
                    history_entries=config.limits.history_entries,
                )
                return _result(
                    rollup,
                    started=started,
                    budget=budget,
                    downloaded=0,
                    reused=len(listed),
                    idempotent=True,
                )
            manifest["state"] = "partial"
            manifest.pop("rollup", None)

        scopes = manifest.setdefault("scopes", {})
        if not isinstance(scopes, dict):
            raise ValidationError("manifest scopes must be an object")
        try:
            for country in config.countries:
                for test in config.tests:
                    scope_name = f"{country}/{test}"
                    scope = scopes.get(scope_name)
                    if isinstance(scope, dict) and scope.get("listed") is True:
                        continue
                    listed = list_scope_objects(
                        config,
                        target,
                        country,
                        test,
                        opener=opener,
                        halt_check=kill.is_halted,
                    )
                    already_listed = len(_objects_from_manifest_scopes(manifest))
                    if already_listed + len(listed) > config.limits.max_objects_per_run:
                        raise LimitExceeded(
                            "hour lists more than the "
                            f"{config.limits.max_objects_per_run}-object cap"
                        )
                    scopes[scope_name] = {
                        "listed": True,
                        "objects": [item.as_manifest_dict() for item in listed],
                    }
                    manifest["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    _write_manifest(manifest_path, manifest)

            listed = _objects_from_manifest_scopes(manifest)
            if len(listed) > config.limits.max_objects_per_run:
                raise LimitExceeded(
                    f"hour lists {len(listed)} objects; cap is "
                    f"{config.limits.max_objects_per_run}"
                )
            oversized = [item for item in listed if item.size > config.limits.object_bytes]
            if oversized:
                raise LimitExceeded(
                    f"hour contains an object over the {config.limits.object_bytes}-byte cap"
                )

            reusable: dict[str, dict[str, Any]] = {}
            pending: list[S3Object] = []
            for item in listed:
                entry = _reuse_entry(config, warehouse_root, item, objects_map.get(item.key))
                if entry is None:
                    pending.append(item)
                else:
                    reusable[item.key] = entry
                    if objects_map.get(item.key) != entry:
                        objects_map[item.key] = entry
                        _write_manifest(manifest_path, manifest)

            planned = sum(item.size for item in pending)
            if planned > config.limits.run_bytes:
                raise LimitExceeded(
                    f"hour needs {planned} download bytes; run cap is {config.limits.run_bytes}"
                )
            usage = usage_provider(warehouse_root)
            if usage + planned > config.limits.source_quota_bytes:
                raise LimitExceeded(
                    "OONI warehouse quota of "
                    f"{config.limits.source_quota_bytes} bytes would be exceeded"
                )
            free = int(disk_usage(warehouse_root).free)
            if free - planned < config.limits.free_space_reserve_bytes:
                raise LimitExceeded(
                    "free-space reserve of "
                    f"{config.limits.free_space_reserve_bytes} bytes would be crossed"
                )

            downloaded = 0
            for item in pending:
                # Recheck the global reserve immediately before each large
                # allocation; other services can consume disk after preflight.
                current_free = int(disk_usage(warehouse_root).free)
                if current_free - item.size < config.limits.free_space_reserve_bytes:
                    raise LimitExceeded("free-space reserve would be crossed during download")
                entry = download_object(
                    config,
                    item,
                    _safe_destination(warehouse_root, item),
                    opener=opener,
                    budget=budget,
                    halt_check=kill.is_halted,
                )
                objects_map[item.key] = entry
                downloaded += 1
                manifest["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                _write_manifest(manifest_path, manifest)

            selected_entries = {
                item.key: objects_map[item.key]
                for item in listed
                if item.key in objects_map
            }
            if len(selected_entries) != len(listed):
                raise ValidationError("not every listed object has a validated manifest entry")
            if kill.is_halted():
                raise CollectionHalted("global kill switch is engaged")
            generated_at = datetime.now(UTC)
            rollup = _build_rollup(
                config,
                target,
                selected_entries,
                generated_at=generated_at,
            )
            manifest.update({
                "state": "complete",
                "updated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "completed_at": generated_at.isoformat().replace("+00:00", "Z"),
                "rollup": rollup,
            })
            manifest.pop("last_error", None)
            _write_manifest(manifest_path, manifest)
            publish_rollup(
                rollup,
                readings=readings,
                history_entries=config.limits.history_entries,
            )
            return _result(
                rollup,
                started=started,
                budget=budget,
                downloaded=downloaded,
                reused=len(reusable),
                idempotent=False,
            )
        except Exception as exc:
            manifest["state"] = "partial"
            manifest["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            manifest["last_error"] = type(exc).__name__
            try:
                _write_manifest(manifest_path, manifest)
            except Exception:
                pass
            if isinstance(exc, CollectionHalted):
                return {
                    "collector": "ooni-bulk",
                    "status": "halted",
                    "records_collected": 0,
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "hour": format_hour(target),
                    "bytes_downloaded": budget.consumed,
                    "error": "global kill switch is engaged",
                }
            raise


def warehouse_enabled() -> bool:
    value = os.getenv("PALIMPSEST_OONI_BULK_ENABLED", "")
    return value.strip().lower() in _TRUTHY
