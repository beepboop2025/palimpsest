"""Strict runtime support for Lab Evidence Envelope v1.

The public JSON Schema is the structural contract.  This module implements the
same shape with the standard library and adds the behavioral rules that JSON
Schema cannot express: strict JSON parsing, timestamp and decimal comparisons,
source-group consistency, publication gates, digest verification, and graph
validation for corrections.

Source URIs are inert provenance labels.  Nothing in this module performs
network I/O or dereferences a URI.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from evidence.capsule import (
    CapsuleError,
    canonical_bytes as _capsule_canonical_bytes,
    strict_json_loads as _capsule_strict_json_loads,
)


SCHEMA_VERSION = "lab-evidence-envelope/v1"
CANONICALIZATION = "palimpsest-json-sorted-utf8-v1"

# A maximally populated v1 record is comfortably below this limit.  The bound
# is intentionally applied before parsing and again after canonicalization.
MAX_ENVELOPE_BYTES = 512 * 1024
MAX_ENVELOPE_SET_BYTES = 16 * 1024 * 1024
MAX_ENVELOPES = 512
MAX_DECIMAL_CHARS = 256

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_FRACTION = re.compile(r"^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$")
_UNIT = re.compile(r"^[A-Za-z][A-Za-z0-9._/%*^-]*$")
_RFC3339_UTC = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,18}))?(Z|\+00:00)$"
)
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_URN = re.compile(
    r"^urn:([A-Za-z0-9](?:[A-Za-z0-9-]{0,30}[A-Za-z0-9])?):(.+)$"
)

_ROOT_REQUIRED = frozenset({
    "schema",
    "record_id",
    "signal_id",
    "event_time",
    "knowledge_time",
    "publication_time",
    "jurisdiction",
    "measure",
    "evidence_status",
    "measured_fraction",
    "support_level",
    "source_groups",
    "source_refs",
    "hashes",
    "redistribution_status",
    "public_value_allowed",
    "privacy_tier",
    "review_status",
    "contains_exact_iocs",
    "contains_raw_messages",
    "limitations",
    "supersedes",
})
_ROOT_OPTIONAL = frozenset({
    "dimensions", "method", "license", "reviewed_at",
})

_EVIDENCE_STATUSES = frozenset({"OBSERVED", "DERIVED", "SCENARIO"})
_SUPPORT_LEVELS = frozenset({
    "DIRECT_OBSERVATION",
    "CORROBORATED_OBSERVATION",
    "REPORTED_OBSERVATION",
    "DERIVED_ESTIMATE",
    "TYPOLOGY_MATCH",
    "CORROBORATED_LEAD",
    "DIRECT_LINK",
    "SCENARIO_ONLY",
    "NOT_ASSESSED",
})
_EVIDENCE_CLASSES = frozenset({
    "OFFICIAL_STATISTIC",
    "MARKET_OBSERVATION",
    "RESEARCH_DATASET",
    "REVIEWED_REPORT",
    "CONSENT_SCOPED_AGGREGATE",
    "METHOD_OR_ASSUMPTION",
})
_REDISTRIBUTION_STATUSES = frozenset({
    "OPEN", "ATTRIBUTION_REQUIRED", "LINK_ONLY", "RESTRICTED", "UNKNOWN",
})
_PRIVACY_TIERS = frozenset({"PUBLIC_AGGREGATE", "CONTROLLED_AGGREGATE"})
_REVIEW_STATUSES = frozenset({
    "UNREVIEWED",
    "MACHINE_VALIDATED",
    "HUMAN_REVIEW_REQUIRED",
    "HUMAN_REVIEWED",
    "REJECTED",
})
_INTERVAL_KINDS = frozenset({"RANGE", "CONFIDENCE", "CREDIBLE", "SCENARIO"})
_CORROBORATED_LEVELS = frozenset({
    "CORROBORATED_OBSERVATION", "CORROBORATED_LEAD",
})


class LabEvidenceError(ValueError):
    """An envelope is malformed, inconsistent, or fails admission policy."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def strict_json_loads(
    data: bytes | str,
    *,
    maximum_bytes: int = MAX_ENVELOPE_BYTES,
    purpose: str = "lab evidence envelope",
) -> Any:
    """Parse bounded UTF-8 JSON with duplicate and non-finite rejection."""

    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise LabEvidenceError("maximum_bytes must be a positive integer")
    if type(data) is bytes:
        encoded = data
    elif type(data) is str:
        try:
            encoded = data.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise LabEvidenceError(f"{purpose} is not Unicode scalar text") from exc
    else:
        raise LabEvidenceError(f"{purpose} must be bytes or text JSON")
    if len(encoded) > maximum_bytes:
        raise LabEvidenceError(
            f"{purpose} exceeds the {maximum_bytes}-byte input limit"
        )
    try:
        return _capsule_strict_json_loads(encoded)
    except CapsuleError as exc:
        raise LabEvidenceError(f"invalid {purpose}: {exc}") from exc


def canonical_json_bytes(value: object) -> bytes:
    """Return the protocol's sorted-key, compact UTF-8 canonical encoding."""

    try:
        encoded = _capsule_canonical_bytes(value)
    except CapsuleError as exc:
        raise LabEvidenceError(f"value cannot be canonicalized: {exc}") from exc
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise LabEvidenceError(
            f"canonical value exceeds the {MAX_ENVELOPE_BYTES}-byte limit"
        )
    return encoded


def _scalar_text(value: object, path: str, minimum: int, maximum: int) -> str:
    if type(value) is not str:
        raise LabEvidenceError(f"{path} must be a string")
    if not minimum <= len(value) <= maximum:
        raise LabEvidenceError(
            f"{path} length must be between {minimum} and {maximum} characters"
        )
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise LabEvidenceError(f"{path} contains a lone Unicode surrogate")
    return value


def _identifier(value: object, path: str) -> str:
    text = _scalar_text(value, path, 1, 128)
    if not _IDENTIFIER.fullmatch(text):
        raise LabEvidenceError(f"{path} is not a v1 identifier")
    return text


def _digest(value: object, path: str) -> str:
    text = _scalar_text(value, path, 64, 64)
    if not _SHA256.fullmatch(text):
        raise LabEvidenceError(f"{path} must be a lowercase SHA-256 digest")
    return text


def _enum(value: object, allowed: frozenset[str], path: str) -> str:
    if type(value) is not str or value not in allowed:
        raise LabEvidenceError(f"{path} has an unsupported value")
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise LabEvidenceError(f"{path} must be a boolean")
    return value


def _exact_object(
    value: object,
    required: frozenset[str],
    optional: frozenset[str],
    path: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise LabEvidenceError(f"{path} must be a JSON object")
    if any(type(key) is not str for key in value):
        raise LabEvidenceError(f"{path} object keys must be strings")
    actual = set(value)
    missing = sorted(required - actual)
    unknown = sorted(actual - required - optional)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise LabEvidenceError(f"{path} has " + " and ".join(details))
    return value


def _array(value: object, path: str, minimum: int, maximum: int) -> list[Any]:
    if type(value) is not list:
        raise LabEvidenceError(f"{path} must be a JSON array")
    if not minimum <= len(value) <= maximum:
        raise LabEvidenceError(
            f"{path} must contain between {minimum} and {maximum} items"
        )
    return value


def _unique_strings(
    value: object,
    path: str,
    *,
    minimum: int,
    maximum: int,
    item_validator,
) -> list[str]:
    items = _array(value, path, minimum, maximum)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        checked = item_validator(item, f"{path}[{index}]")
        if checked in seen:
            raise LabEvidenceError(f"{path} contains duplicate value {checked!r}")
        seen.add(checked)
        result.append(checked)
    return result


def _timestamp(value: object, path: str) -> tuple[datetime, Decimal]:
    text = _scalar_text(value, path, 20, 48)
    match = _RFC3339_UTC.fullmatch(text)
    if match is None:
        raise LabEvidenceError(
            f"{path} must be an RFC 3339 UTC timestamp ending in Z or +00:00"
        )
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    try:
        instant = datetime(
            year, month, day, hour, minute, second, tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise LabEvidenceError(f"{path} is not a valid calendar instant") from exc
    fraction_digits = match.group(7) or "0"
    fraction = Decimal(f"0.{fraction_digits}")
    return instant, fraction


def _decimal(value: object, path: str) -> Decimal:
    text = _scalar_text(value, path, 1, MAX_DECIMAL_CHARS)
    if not _DECIMAL.fullmatch(text):
        raise LabEvidenceError(f"{path} is not a canonical decimal string")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise LabEvidenceError(f"{path} is not a decimal") from exc
    if not parsed.is_finite():  # defensive if the grammar changes
        raise LabEvidenceError(f"{path} must be finite")
    return parsed


def _fraction(value: object, path: str) -> Decimal:
    text = _scalar_text(value, path, 1, MAX_DECIMAL_CHARS)
    if not _FRACTION.fullmatch(text):
        raise LabEvidenceError(f"{path} is not a canonical fraction from 0 through 1")
    parsed = Decimal(text)
    if not Decimal(0) <= parsed <= Decimal(1):  # defensive and explicit
        raise LabEvidenceError(f"{path} must be from 0 through 1")
    return parsed


def _validate_percent_escapes(value: str, path: str) -> None:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        escape = value[index + 1:index + 3]
        if len(escape) != 2 or not re.fullmatch(r"[0-9A-F]{2}", escape):
            raise LabEvidenceError(
                f"{path} percent escapes must use two uppercase hexadecimal digits"
            )
        decoded = int(escape, 16)
        if decoded < 0x20 or decoded == 0x7F:
            raise LabEvidenceError(f"{path} contains an encoded control character")
        index += 3


def _validate_https_uri(value: str, path: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LabEvidenceError(f"{path} is not a valid HTTPS URL") from exc
    if parsed.scheme != "https" or not value.startswith("https://"):
        raise LabEvidenceError(f"{path} must use lowercase https")
    if not parsed.hostname:
        raise LabEvidenceError(f"{path} must have a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise LabEvidenceError(f"{path} must not contain credentials")
    if parsed.fragment or "#" in value:
        raise LabEvidenceError(f"{path} must not contain a fragment")
    if port not in (None, 443):
        raise LabEvidenceError(f"{path} must not use a non-default port")

    authority = value.split("//", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if authority.endswith(":"):
        raise LabEvidenceError(f"{path} contains an empty port")
    if authority != authority.lower():
        raise LabEvidenceError(f"{path} hostname must be lowercase")
    if port == 443:
        if authority.startswith("["):
            close = authority.find("]")
            port_text = authority[close + 2:] if authority[close + 1:].startswith(":") else ""
        else:
            port_text = authority.rsplit(":", 1)[1]
        if port_text != "443":
            raise LabEvidenceError(f"{path} port is not canonical")

    hostname = parsed.hostname
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise LabEvidenceError(
            f"{path} hostname must use ASCII or punycode"
        ) from exc
    if hostname != hostname.lower():
        raise LabEvidenceError(f"{path} hostname must be lowercase")
    if "%" in hostname:
        raise LabEvidenceError(f"{path} IPv6 scope identifiers are forbidden")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if len(hostname) > 253 or hostname.endswith("."):
            raise LabEvidenceError(f"{path} hostname is not canonical DNS")
        if not all(_DNS_LABEL.fullmatch(label) for label in hostname.split(".")):
            raise LabEvidenceError(f"{path} hostname is not valid DNS")
    else:
        if address.version == 6:
            bracketed = f"[{address.compressed}]"
            if not authority.startswith(bracketed):
                raise LabEvidenceError(f"{path} IPv6 hostname is not canonical")

    _validate_percent_escapes(value, path)
    decoded_path = unquote(parsed.path)
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise LabEvidenceError(f"{path} path must not contain dot segments")


def _validate_urn(value: str, path: str) -> None:
    match = _URN.fullmatch(value)
    if match is None:
        raise LabEvidenceError(f"{path} is not a bounded URN")
    if match.group(1).lower() == "urn":
        raise LabEvidenceError(f"{path} uses the reserved URN namespace identifier")
    # URNs remain inert, but visible-ASCII and canonical escaping keep their
    # byte identity portable across producers.
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise LabEvidenceError(f"{path} URN must contain visible ASCII characters")
    _validate_percent_escapes(value, path)


def _source_uri(value: object, path: str) -> str:
    text = _scalar_text(value, path, 1, 2_048)
    if "\\" in text or any(character.isspace() for character in text):
        raise LabEvidenceError(f"{path} contains forbidden whitespace or backslash")
    if text.startswith("https://"):
        _validate_https_uri(text, path)
    elif text.startswith("urn:"):
        _validate_urn(text, path)
    else:
        raise LabEvidenceError(f"{path} must be a safe HTTPS URL or inert URN")
    return text


def _validate_jurisdiction(value: object) -> None:
    item = _exact_object(
        value,
        frozenset({"scheme", "code"}),
        frozenset({"label"}),
        "envelope.jurisdiction",
    )
    scheme = _enum(
        item["scheme"],
        frozenset({"ISO-3166-1-alpha-2", "ISO-3166-1-alpha-3", "UN-M49", "SPECIAL"}),
        "envelope.jurisdiction.scheme",
    )
    code = _scalar_text(item["code"], "envelope.jurisdiction.code", 1, 7)
    patterns = {
        "ISO-3166-1-alpha-2": re.compile(r"^[A-Z]{2}$"),
        "ISO-3166-1-alpha-3": re.compile(r"^[A-Z]{3}$"),
        "UN-M49": re.compile(r"^[0-9]{3}$"),
    }
    if scheme == "SPECIAL":
        if code not in {"GLOBAL", "MULTI", "UNKNOWN"}:
            raise LabEvidenceError("envelope.jurisdiction.code is not a special code")
    elif not patterns[scheme].fullmatch(code):
        raise LabEvidenceError(
            "envelope.jurisdiction.code does not match its declared scheme"
        )
    if "label" in item:
        _scalar_text(item["label"], "envelope.jurisdiction.label", 1, 128)


def _validate_dimensions(value: object) -> None:
    item = _exact_object(
        value,
        frozenset(),
        frozenset({"substance_ids", "typology_ids"}),
        "envelope.dimensions",
    )
    for field in ("substance_ids", "typology_ids"):
        if field in item:
            _unique_strings(
                item[field],
                f"envelope.dimensions.{field}",
                minimum=0,
                maximum=32,
                item_validator=_identifier,
            )


def _validate_measure(value: object) -> bool:
    if type(value) is not dict:
        raise LabEvidenceError("envelope.measure must be a JSON object")
    if "value" in value and "interval" in value:
        raise LabEvidenceError("envelope.measure must not contain both value and interval")
    has_interval = "interval" in value
    expected = (
        frozenset({"type", "interval", "unit"})
        if has_interval
        else frozenset({"type", "value", "unit"})
    )
    item = _exact_object(value, expected, frozenset(), "envelope.measure")
    _identifier(item["type"], "envelope.measure.type")
    unit = _scalar_text(item["unit"], "envelope.measure.unit", 1, 64)
    if not _UNIT.fullmatch(unit):
        raise LabEvidenceError("envelope.measure.unit is not a v1 unit")
    if not has_interval:
        _decimal(item["value"], "envelope.measure.value")
        return False

    interval = _exact_object(
        item["interval"],
        frozenset({"lower", "upper", "kind"}),
        frozenset({"level"}),
        "envelope.measure.interval",
    )
    lower = _decimal(interval["lower"], "envelope.measure.interval.lower")
    upper = _decimal(interval["upper"], "envelope.measure.interval.upper")
    if lower > upper:
        raise LabEvidenceError("envelope.measure.interval lower exceeds upper")
    _enum(interval["kind"], _INTERVAL_KINDS, "envelope.measure.interval.kind")
    if "level" in interval:
        _fraction(interval["level"], "envelope.measure.interval.level")
        return True
    return False


def _validate_source_ref(value: object, index: int) -> tuple[str, str]:
    path = f"envelope.source_refs[{index}]"
    item = _exact_object(
        value,
        frozenset({
            "id", "group_id", "publisher", "uri", "retrieved_at",
            "evidence_class", "content_sha256",
        }),
        frozenset(),
        path,
    )
    ref_id = _identifier(item["id"], f"{path}.id")
    group_id = _identifier(item["group_id"], f"{path}.group_id")
    _scalar_text(item["publisher"], f"{path}.publisher", 1, 128)
    _source_uri(item["uri"], f"{path}.uri")
    _timestamp(item["retrieved_at"], f"{path}.retrieved_at")
    _enum(item["evidence_class"], _EVIDENCE_CLASSES, f"{path}.evidence_class")
    _digest(item["content_sha256"], f"{path}.content_sha256")
    return ref_id, group_id


def _validate_source_refs(value: object) -> tuple[list[dict[str, Any]], set[str]]:
    refs = _array(value, "envelope.source_refs", 1, 64)
    ids: set[str] = set()
    groups: set[str] = set()
    for index, ref in enumerate(refs):
        ref_id, group_id = _validate_source_ref(ref, index)
        if ref_id in ids:
            raise LabEvidenceError(
                f"envelope.source_refs contains duplicate id {ref_id!r}"
            )
        ids.add(ref_id)
        groups.add(group_id)
    return refs, groups


def _validate_method(value: object) -> None:
    item = _exact_object(
        value,
        frozenset({"id", "version", "input_record_ids", "assumptions"}),
        frozenset(),
        "envelope.method",
    )
    _identifier(item["id"], "envelope.method.id")
    _scalar_text(item["version"], "envelope.method.version", 1, 64)
    _unique_strings(
        item["input_record_ids"],
        "envelope.method.input_record_ids",
        minimum=0,
        maximum=128,
        item_validator=_identifier,
    )
    _unique_strings(
        item["assumptions"],
        "envelope.method.assumptions",
        minimum=0,
        maximum=32,
        item_validator=lambda item, path: _scalar_text(item, path, 1, 512),
    )


def _validate_hashes(value: object) -> dict[str, Any]:
    item = _exact_object(
        value,
        frozenset({"algorithm", "record_sha256", "source_set_sha256"}),
        frozenset({"method_sha256", "artifact_sha256"}),
        "envelope.hashes",
    )
    if item["algorithm"] != "sha256":
        raise LabEvidenceError("envelope.hashes.algorithm must be 'sha256'")
    _digest(item["record_sha256"], "envelope.hashes.record_sha256")
    _digest(item["source_set_sha256"], "envelope.hashes.source_set_sha256")
    if "method_sha256" in item:
        _digest(item["method_sha256"], "envelope.hashes.method_sha256")
    if "artifact_sha256" in item:
        _unique_strings(
            item["artifact_sha256"],
            "envelope.hashes.artifact_sha256",
            minimum=0,
            maximum=64,
            item_validator=_digest,
        )
    return item


def _canonical_source_projection(source_refs: object) -> list[dict[str, Any]]:
    refs, _ = _validate_source_refs(source_refs)
    return deepcopy(sorted(refs, key=lambda item: item["id"]))


def compute_source_set_sha256(source_refs: object) -> str:
    """Hash the complete source-ref array after sorting records by ``id``."""

    projection = _canonical_source_projection(source_refs)
    return _sha256(canonical_json_bytes(projection))


def compute_record_sha256(envelope: object) -> str:
    """Hash an envelope after removing only ``hashes.record_sha256``."""

    if type(envelope) is not dict:
        raise LabEvidenceError("envelope must be a JSON object")
    projection = deepcopy(envelope)
    hashes = projection.get("hashes")
    if type(hashes) is not dict:
        raise LabEvidenceError("envelope.hashes must be a JSON object")
    hashes.pop("record_sha256", None)
    return _sha256(canonical_json_bytes(projection))


def _coerce_envelope(value: object) -> dict[str, Any]:
    if type(value) in (bytes, str):
        parsed = strict_json_loads(value)
    elif type(value) is dict:
        parsed = deepcopy(value)
    else:
        raise LabEvidenceError("envelope must be a JSON object or bounded JSON")
    if type(parsed) is not dict:
        raise LabEvidenceError("envelope must be a JSON object")
    return parsed


def _validate_document(document: dict[str, Any], *, verify_hashes: bool) -> None:
    envelope = _exact_object(
        document, _ROOT_REQUIRED, _ROOT_OPTIONAL, "envelope"
    )
    if envelope["schema"] != SCHEMA_VERSION:
        raise LabEvidenceError("envelope.schema is not lab-evidence-envelope/v1")
    record_id = _identifier(envelope["record_id"], "envelope.record_id")
    _identifier(envelope["signal_id"], "envelope.signal_id")

    event = _timestamp(envelope["event_time"], "envelope.event_time")
    knowledge = _timestamp(envelope["knowledge_time"], "envelope.knowledge_time")
    publication = _timestamp(
        envelope["publication_time"], "envelope.publication_time"
    )
    if not event <= knowledge <= publication:
        raise LabEvidenceError(
            "timestamps must satisfy event_time <= knowledge_time <= publication_time"
        )

    _validate_jurisdiction(envelope["jurisdiction"])
    if "dimensions" in envelope:
        _validate_dimensions(envelope["dimensions"])
    interval_has_level = _validate_measure(envelope["measure"])

    evidence_status = _enum(
        envelope["evidence_status"], _EVIDENCE_STATUSES, "envelope.evidence_status"
    )
    measured_fraction = _fraction(
        envelope["measured_fraction"], "envelope.measured_fraction"
    )
    support_level = _enum(
        envelope["support_level"], _SUPPORT_LEVELS, "envelope.support_level"
    )

    if "method" in envelope:
        _validate_method(envelope["method"])
    if interval_has_level and "method" not in envelope:
        raise LabEvidenceError(
            "an interval level requires a versioned method defining its meaning"
        )
    if evidence_status == "OBSERVED":
        if envelope["measured_fraction"] != "1":
            raise LabEvidenceError("OBSERVED records require measured_fraction '1'")
        if support_level in {"DERIVED_ESTIMATE", "SCENARIO_ONLY"}:
            raise LabEvidenceError("OBSERVED record has an incompatible support level")
    elif evidence_status == "DERIVED":
        if "method" not in envelope:
            raise LabEvidenceError("DERIVED records require a method")
        if support_level == "SCENARIO_ONLY":
            raise LabEvidenceError("DERIVED records cannot be SCENARIO_ONLY")
    else:
        if "method" not in envelope:
            raise LabEvidenceError("SCENARIO records require a method")
        if envelope["measured_fraction"] != "0":
            raise LabEvidenceError("SCENARIO records require measured_fraction '0'")
        if support_level != "SCENARIO_ONLY":
            raise LabEvidenceError("SCENARIO records require SCENARIO_ONLY support")
        if not envelope["method"]["assumptions"]:
            raise LabEvidenceError("SCENARIO records require at least one assumption")
    # Keep the Decimal parse live even where the lexical form has a stronger
    # status-specific rule; it documents the lineage range invariant.
    if not Decimal(0) <= measured_fraction <= Decimal(1):  # pragma: no cover
        raise LabEvidenceError("measured_fraction is outside 0 through 1")

    source_groups = _unique_strings(
        envelope["source_groups"],
        "envelope.source_groups",
        minimum=1,
        maximum=32,
        item_validator=_identifier,
    )
    _, referenced_groups = _validate_source_refs(envelope["source_refs"])
    declared_groups = set(source_groups)
    dangling = sorted(referenced_groups - declared_groups)
    unused = sorted(declared_groups - referenced_groups)
    if dangling:
        raise LabEvidenceError(
            f"source_refs use undeclared source groups {dangling}"
        )
    if unused:
        raise LabEvidenceError(f"source_groups are not backed by source refs {unused}")
    if support_level in _CORROBORATED_LEVELS and len(declared_groups) < 2:
        raise LabEvidenceError(
            f"{support_level} requires at least two independent source groups"
        )

    hashes = _validate_hashes(envelope["hashes"])
    redistribution = _enum(
        envelope["redistribution_status"],
        _REDISTRIBUTION_STATUSES,
        "envelope.redistribution_status",
    )
    public_allowed = _boolean(
        envelope["public_value_allowed"], "envelope.public_value_allowed"
    )
    if "license" in envelope:
        _scalar_text(envelope["license"], "envelope.license", 1, 256)
    privacy = _enum(
        envelope["privacy_tier"], _PRIVACY_TIERS, "envelope.privacy_tier"
    )
    review = _enum(
        envelope["review_status"], _REVIEW_STATUSES, "envelope.review_status"
    )
    if review == "HUMAN_REVIEWED" and "reviewed_at" not in envelope:
        raise LabEvidenceError("HUMAN_REVIEWED records require reviewed_at")
    if "reviewed_at" in envelope:
        _timestamp(envelope["reviewed_at"], "envelope.reviewed_at")
    if public_allowed:
        if redistribution not in {"OPEN", "ATTRIBUTION_REQUIRED"}:
            raise LabEvidenceError(
                "public values require OPEN or ATTRIBUTION_REQUIRED redistribution"
            )
        if privacy != "PUBLIC_AGGREGATE":
            raise LabEvidenceError("public values require PUBLIC_AGGREGATE privacy")
        if review not in {"MACHINE_VALIDATED", "HUMAN_REVIEWED"}:
            raise LabEvidenceError(
                "public values require MACHINE_VALIDATED or HUMAN_REVIEWED status"
            )
    if redistribution in {"LINK_ONLY", "RESTRICTED", "UNKNOWN"} and public_allowed:
        raise LabEvidenceError("non-redistributable values must fail closed")

    if envelope["contains_exact_iocs"] is not False:
        raise LabEvidenceError("contains_exact_iocs must be literal false")
    if envelope["contains_raw_messages"] is not False:
        raise LabEvidenceError("contains_raw_messages must be literal false")
    _unique_strings(
        envelope["limitations"],
        "envelope.limitations",
        minimum=1,
        maximum=32,
        item_validator=lambda item, path: _scalar_text(item, path, 1, 512),
    )
    supersedes = _unique_strings(
        envelope["supersedes"],
        "envelope.supersedes",
        minimum=0,
        maximum=32,
        item_validator=_identifier,
    )
    if record_id in supersedes:
        raise LabEvidenceError("record_id must not supersede itself")

    # Canonicalization also rejects floats, invalid Unicode scalar text, and
    # structures outside the named v1 canonical domain.
    canonical_json_bytes(envelope)
    if verify_hashes:
        expected_source_hash = compute_source_set_sha256(envelope["source_refs"])
        if hashes["source_set_sha256"] != expected_source_hash:
            raise LabEvidenceError(
                "envelope.hashes.source_set_sha256 does not match source_refs"
            )
        expected_record_hash = compute_record_sha256(envelope)
        if hashes["record_sha256"] != expected_record_hash:
            raise LabEvidenceError(
                "envelope.hashes.record_sha256 does not match the envelope"
            )


def validate_envelope(value: object) -> dict[str, Any]:
    """Validate one envelope and return an isolated, canonical-domain copy.

    Supersession existence is necessarily a set-level property.  A single
    envelope rejects self-supersession; use :func:`validate_envelope_set` before
    admitting a correction chain.
    """

    document = _coerce_envelope(value)
    _validate_document(document, verify_hashes=True)
    return document


def seal_envelope_hashes(value: object) -> dict[str, Any]:
    """Return a validated copy with canonical source-set and record digests.

    This helper is for trusted producers.  It does not fetch or authenticate
    sources; it only computes the two envelope-level identities.
    """

    document = _coerce_envelope(value)
    hashes = document.get("hashes")
    if type(hashes) is not dict:
        raise LabEvidenceError("envelope.hashes must be a JSON object")
    hashes["source_set_sha256"] = "0" * 64
    hashes["record_sha256"] = "0" * 64
    _validate_document(document, verify_hashes=False)
    hashes["source_set_sha256"] = compute_source_set_sha256(document["source_refs"])
    hashes["record_sha256"] = compute_record_sha256(document)
    _validate_document(document, verify_hashes=True)
    return document


def _validate_supersession_graph(records: Sequence[dict[str, Any]]) -> None:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = record["record_id"]
        if record_id in by_id:
            raise LabEvidenceError(
                f"envelope set contains duplicate record_id {record_id!r}"
            )
        by_id[record_id] = record

    for record in records:
        missing = sorted(set(record["supersedes"]) - set(by_id))
        if missing:
            raise LabEvidenceError(
                f"{record['record_id']!r} supersedes records absent from the admitted set: {missing}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(record_id: str) -> None:
        if record_id in visiting:
            raise LabEvidenceError("envelope supersession graph contains a cycle")
        if record_id in visited:
            return
        visiting.add(record_id)
        for older_id in by_id[record_id]["supersedes"]:
            visit(older_id)
        visiting.remove(record_id)
        visited.add(record_id)

    for record_id in by_id:
        visit(record_id)


def validate_envelope_set(value: object) -> tuple[dict[str, Any], ...]:
    """Validate a bounded admitted record set and its supersession graph."""

    if type(value) in (bytes, str):
        parsed = strict_json_loads(
            value,
            maximum_bytes=MAX_ENVELOPE_SET_BYTES,
            purpose="lab evidence envelope set",
        )
    elif type(value) in (list, tuple):
        parsed = list(value)
    else:
        raise LabEvidenceError("envelope set must be an array or bounded JSON")
    records = _array(parsed, "envelope set", 0, MAX_ENVELOPES)
    validated = tuple(validate_envelope(record) for record in records)
    total_bytes = sum(len(canonical_json_bytes(record)) for record in validated)
    if total_bytes > MAX_ENVELOPE_SET_BYTES:
        raise LabEvidenceError(
            f"canonical envelope set exceeds the {MAX_ENVELOPE_SET_BYTES}-byte limit"
        )
    _validate_supersession_graph(validated)
    return validated


def _read_bounded(path: str | Path, maximum_bytes: int, purpose: str) -> bytes:
    candidate = Path(path)
    try:
        with candidate.open("rb") as handle:
            payload = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise LabEvidenceError(f"{purpose} cannot be read: {exc}") from exc
    if len(payload) > maximum_bytes:
        raise LabEvidenceError(f"{purpose} exceeds the {maximum_bytes}-byte limit")
    return payload


def load_envelope(path: str | Path) -> dict[str, Any]:
    """Read and validate one bounded envelope file without network access."""

    payload = _read_bounded(path, MAX_ENVELOPE_BYTES, "lab evidence envelope")
    return validate_envelope(payload)


def load_envelope_set(path: str | Path) -> tuple[dict[str, Any], ...]:
    """Read and validate a bounded JSON array of envelopes."""

    payload = _read_bounded(
        path, MAX_ENVELOPE_SET_BYTES, "lab evidence envelope set"
    )
    return validate_envelope_set(payload)


__all__ = [
    "CANONICALIZATION",
    "MAX_DECIMAL_CHARS",
    "MAX_ENVELOPE_BYTES",
    "MAX_ENVELOPE_SET_BYTES",
    "MAX_ENVELOPES",
    "SCHEMA_VERSION",
    "LabEvidenceError",
    "canonical_json_bytes",
    "compute_record_sha256",
    "compute_source_set_sha256",
    "load_envelope",
    "load_envelope_set",
    "seal_envelope_hashes",
    "strict_json_loads",
    "validate_envelope",
    "validate_envelope_set",
]
