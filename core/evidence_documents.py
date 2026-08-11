"""Private, immutable, rights-aware evidence documents.

``EvidenceDocumentStore`` accepts bytes that a caller has already collected.  It
does not fetch URLs.  The bytes and their canonical manifest are installed with
create-only atomic filesystem operations; the manifest is the commit record and
is always written last.

Training access is intentionally narrower than archival access.  The v1 text
policy includes only UTF-8 textual documents admitted by an explicit immutable
rights-decision ledger and knowable, collected, and accepted by the trusted
store clock no later than the requested ``as_of`` timestamp.  Manifest-era
rights are provenance only; missing, revoked, conflicting, and unknown policy
values fail closed.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import ipaddress
import os
import re
import stat
import time
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

try:  # POSIX is validated before a store is used.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX Python
    fcntl = None  # type: ignore[assignment]

from evidence.capsule import (
    CANONICALIZATION,
    CapsuleError,
    canonical_bytes as _capsule_canonical_bytes,
    strict_json_loads as _capsule_strict_json_loads,
)

SPEC_VERSION = "palimpsest-evidence-document/v1"
CAPTURE_REQUEST_SPEC_VERSION = "palimpsest-evidence-capture-request/v1"
ACCEPTANCE_RECEIPT_SPEC_VERSION = "palimpsest-evidence-acceptance-receipt/v1"
CUT_SPEC_VERSION = "palimpsest-evidence-training-cut/v1"
RIGHTS_DECISION_SPEC_VERSION = "palimpsest-evidence-rights-decision/v1"
RIGHTS_LEDGER_SPEC_VERSION = "palimpsest-evidence-rights-ledger/v1"
TEXT_TRAINING_POLICY_ID = "palimpsest-full-text-utf8/v1"

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ACCEPTANCE_RECEIPT_BYTES = 128 * 1024
MAX_TRAINING_CONTENT_BYTES = 16 * 1024 * 1024
MAX_TRAINING_CUT_BYTES = 24 * 1024 * 1024
# The shared canonicalizer bounds every JSON collection at 512 members/items.
# Keeping the cut bound identical avoids a schema/serializer split-brain.
MAX_TRAINING_RECORDS = 512
MAX_PROVENANCE_PER_RECORD = 512
MAX_STORED_MANIFESTS = 100_000
MAX_STORED_RECEIPTS = 100_000
MAX_RIGHTS_DECISIONS = 512
MAX_RIGHTS_LEDGER_BYTES = 2 * 1024 * 1024
MAX_SUPERSEDES = 64
MAX_STAGING_FILES = 1_024
MAX_STAGING_INSPECTIONS = 1_024
DEFAULT_STAGING_MAX_AGE_SECONDS = 24 * 60 * 60

TRAINING_USES = frozenset({"prohibited", "metadata_only", "derived_only", "full_text"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_RETENTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LANGUAGE_RE = re.compile(r"^(?:und|[a-z]{2,8}(?:-[a-z0-9]{1,8})*)$")
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HEX_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
_MANIFEST_FILENAME_RE = re.compile(r"^([0-9a-f]{64})\.json$")
_STAGING_INTENT_RE = re.compile(
    r"^\.intent-(content|receipt|manifest)-([0-9a-f]{64})-([0-9a-f]{64})\.tmp$"
)
_LEGACY_STAGING_FILENAME_RE = re.compile(r"^\.partial-[A-Za-z0-9_-]{6,64}\.tmp$")
_RECEIPT_FILENAME_RE = re.compile(r"^([0-9a-f]{64})\.json$")
_DNS_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$"
)

_METADATA_KEYS = frozenset(
    {
        "source",
        "media_type",
        "language",
        "event_time",
        "publication_time",
        "knowledge_time",
        "collected_at",
        "collection",
        "retention_class",
        "rights",
    }
)
_MANIFEST_KEYS = _METADATA_KEYS | frozenset(
    {"spec_version", "canonicalization", "content", "acceptance"}
)
_CONTENT_KEYS = frozenset({"sha256", "byte_size"})
_ACCEPTANCE_BINDING_KEYS = frozenset(
    {"accepted_at", "capture_request_sha256", "receipt_sha256"}
)
_CAPTURE_REQUEST_KEYS = frozenset(
    {"spec_version", "canonicalization", "metadata", "content"}
)
_ACCEPTANCE_RECEIPT_KEYS = frozenset(
    {
        "spec_version",
        "canonicalization",
        "capture_request_sha256",
        "accepted_at",
        "capture_request",
    }
)

_RIGHTS_DECISION_KEYS = frozenset(
    {
        "spec_version",
        "canonicalization",
        "subject",
        "decision_type",
        "training_use",
        "effective_at",
        "knowledge_time",
        "license_or_terms_ref",
        "reason",
        "supersedes",
    }
)
_RIGHTS_LEDGER_KEYS = frozenset({"spec_version", "canonicalization", "decisions"})
_RIGHTS_ENTRY_KEYS = frozenset({"decision_sha256", "decision"})

_TEXT_MEDIA_TYPE_PREFIXES = ("text/",)
_TEXT_MEDIA_TYPES = (
    "application/javascript",
    "application/json",
    "application/x-ndjson",
    "application/xml",
)
_TEXT_MEDIA_TYPE_SUFFIXES = ("+json", "+xml")

_TEXT_TRAINING_POLICY = {
    "id": TEXT_TRAINING_POLICY_ID,
    "purpose": "text_training",
    "rights_actions": {
        "prohibited": "exclude",
        "metadata_only": "exclude",
        "derived_only": "exclude",
        "full_text": "include",
    },
    "unknown_or_missing_rights": "exclude",
    "temporal_cutoff": {
        "manifest_clocks": [
            "knowledge_time",
            "collected_at",
            "acceptance.accepted_at",
        ],
        "comparison": "each_lte_as_of",
        "trusted_store_clock": "acceptance.accepted_at",
        "source_metadata_clocks": ["knowledge_time", "collected_at"],
    },
    "rights_decisions": {
        "subject_key": ["source.id", "content.sha256"],
        "admission_clocks": ["knowledge_time", "effective_at"],
        "comparison": "each_lte_as_of",
        "terminal_rule": "unsuperseded_active_decision",
        "superseded_decisions": "discard",
        "multiple_terminals": "error",
        "manifest_rights": "provenance_only",
        "missing_terminal_decision": "exclude",
    },
    "media_types": {
        "exact": list(_TEXT_MEDIA_TYPES),
        "type_prefixes": list(_TEXT_MEDIA_TYPE_PREFIXES),
        "structured_syntax_suffixes": list(_TEXT_MEDIA_TYPE_SUFFIXES),
        "parameters": "forbidden_at_ingest",
        "content_encoding": "utf-8",
    },
    "deduplication": {
        "key": ["source.id", "content.sha256"],
        "output": "one_record_per_key",
        "provenance": "all_admitted_manifests_sorted_by_sha256",
        "manifest_rights": "preserve_without_authorizing",
    },
}


class EvidenceDocumentError(ValueError):
    """An evidence document, store, or requested operation is invalid."""


class StoreSafetyError(EvidenceDocumentError):
    """The configured store path is not a safe, private filesystem boundary."""


class IntegrityError(EvidenceDocumentError):
    """Stored content or a manifest does not match its immutable identity."""


class TrainingPolicyError(EvidenceDocumentError):
    """A requested training access is not permitted by the v1 policy."""


class RightsConflictError(TrainingPolicyError):
    """Applicable rights decisions have more than one unsuperseded terminal."""


class HardLinkUnsupportedError(StoreSafetyError):
    """The store filesystem cannot provide the required atomic hard-link commit."""


class DurabilityError(StoreSafetyError):
    """A required file or directory durability barrier could not be established."""


class AcceptanceClockError(EvidenceDocumentError):
    """The trusted acceptance clock reversed or predates source collection."""


@dataclass(frozen=True)
class StoredEvidenceDocument:
    """The content-addressed result of one successful ingest."""

    manifest_sha256: str
    content_sha256: str
    byte_size: int
    manifest_path: Path
    content_path: Path
    receipt_path: Path
    manifest: dict[str, Any]
    accepted_at: str
    receipt_sha256: str
    content_created: bool
    receipt_created: bool
    manifest_created: bool


@dataclass(frozen=True, slots=True, init=False)
class TrainingCut:
    """A deterministic, canonical point-in-time training cut.

    ``cut_sha256`` is SHA-256 of ``canonical_bytes``.  The canonical document
    itself omits a self-referential hash and contains the version, ``as_of``,
    complete policy, visible rights-ledger projection, and canonically ordered
    records.
    """

    cut_sha256: str
    as_of: str
    canonical_bytes: bytes

    def __init__(self, *, cut_sha256: str, as_of: str, canonical_bytes: bytes) -> None:
        del cut_sha256, as_of, canonical_bytes
        raise TrainingPolicyError(
            "direct TrainingCut construction is private; use "
            "EvidenceDocumentStore.build_training_cut"
        )

    @classmethod
    def _from_validated_builder(
        cls,
        *,
        cut_sha256: str,
        as_of: str,
        canonical_bytes: bytes,
        complete_rights_ledger: Mapping[str, Any] | bytes | str,
        trusted_rights_ledger_sha256: str,
    ) -> TrainingCut:
        """Construct only after binding the cut to the trusted complete ledger."""

        validate_training_cut(
            cut_sha256=cut_sha256,
            as_of=as_of,
            canonical_bytes=canonical_bytes,
            complete_rights_ledger=complete_rights_ledger,
            trusted_rights_ledger_sha256=trusted_rights_ledger_sha256,
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, "cut_sha256", cut_sha256)
        object.__setattr__(instance, "as_of", as_of)
        object.__setattr__(instance, "canonical_bytes", canonical_bytes)
        return instance

    def to_dict(self) -> dict[str, Any]:
        """Return an independent JSON-compatible representation of the cut."""

        if _sha256(self.canonical_bytes) != self.cut_sha256:
            raise IntegrityError("training cut bytes do not match cut_sha256")
        value = strict_json_loads(
            self.canonical_bytes,
            maximum_bytes=MAX_TRAINING_CUT_BYTES,
            purpose="training cut",
        )
        if type(value) is not dict:  # pragma: no cover - guaranteed by builder
            raise IntegrityError("canonical training cut is not an object")
        return value

    @property
    def policy(self) -> dict[str, Any]:
        """Return a fresh policy object that cannot mutate the cut identity."""

        return self.to_dict()["policy"]

    @property
    def rights_ledger(self) -> dict[str, Any]:
        """Return a fresh as-of rights ledger snapshot."""

        return self.to_dict()["rights_ledger"]

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        """Return fresh record objects; mutations never affect canonical bytes."""

        return tuple(self.to_dict()["records"])


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(document: object) -> bytes:
    """Encode the repository's integer-only canonical JSON form.

    This reuses the Evidence Capsule v1 canonicalizer so the named transform has
    one implementation.  Evidence-document resource limits are applied by the
    caller because manifests and training cuts have different bounds.
    """

    try:
        return _capsule_canonical_bytes(document)
    except CapsuleError as exc:
        raise EvidenceDocumentError(str(exc)) from exc


def strict_json_loads(
    data: bytes | str,
    *,
    maximum_bytes: int = MAX_METADATA_BYTES,
    purpose: str = "evidence metadata",
) -> Any:
    """Parse bounded UTF-8 JSON, rejecting duplicate keys and invalid numbers."""

    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise EvidenceDocumentError("maximum_bytes must be a positive integer")
    if type(data) is bytes:
        encoded = data
    elif type(data) is str:
        try:
            encoded = data.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise EvidenceDocumentError(
                f"{purpose} is not Unicode scalar text"
            ) from exc
    else:
        raise EvidenceDocumentError(f"{purpose} must be bytes or text JSON")
    if len(encoded) > maximum_bytes:
        raise EvidenceDocumentError(f"{purpose} exceeds the {maximum_bytes}-byte limit")
    try:
        return _capsule_strict_json_loads(encoded)
    except CapsuleError as exc:
        raise EvidenceDocumentError(f"invalid {purpose}: {exc}") from exc


def _exact_object(value: object, expected: frozenset[str], path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise EvidenceDocumentError(f"{path} must be a JSON object")
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unknown {extra}")
        raise EvidenceDocumentError(f"{path} has " + " and ".join(details))
    return value


def _bounded_string(value: object, path: str, maximum: int) -> str:
    if type(value) is not str or not value:
        raise EvidenceDocumentError(f"{path} must be a non-empty string")
    if len(value) > maximum:
        raise EvidenceDocumentError(f"{path} exceeds {maximum} characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EvidenceDocumentError(f"{path} is not Unicode scalar text") from exc
    if len(encoded) > maximum * 4:
        raise EvidenceDocumentError(f"{path} is too large when encoded as UTF-8")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise EvidenceDocumentError(f"{path} contains a control character")
    return value


def _safe_identifier(value: object, path: str) -> str:
    text = _bounded_string(value, path, 128)
    if not _SAFE_ID_RE.fullmatch(text):
        raise EvidenceDocumentError(f"{path} is not a safe identifier")
    return text


def _sha256_value(value: object, path: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise EvidenceDocumentError(
            f"{path} must be a 64-character lowercase SHA-256 digest"
        )
    return value


def _timestamp(value: object, path: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or not _TIMESTAMP_RE.fullmatch(value):
        raise EvidenceDocumentError(
            f"{path} must be a UTC timestamp with second precision"
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise EvidenceDocumentError(f"{path} is not a real timestamp") from exc
    return value


def _timestamp_value(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _as_of_timestamp(value: str | datetime) -> str:
    if type(value) is str:
        result = _timestamp(value, "as_of")
        assert result is not None
        return result
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise EvidenceDocumentError("as_of datetime must be timezone-aware")
        normalized = value.astimezone(timezone.utc)
        if normalized.microsecond:
            raise EvidenceDocumentError("as_of must have second precision")
        return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")
    raise EvidenceDocumentError("as_of must be a timestamp string or datetime")


def _trusted_timestamp(value: str | datetime, path: str) -> str:
    """Normalize a trusted injected clock value to whole-second UTC."""

    if type(value) is str:
        result = _timestamp(value, path)
        assert result is not None
        return result
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise AcceptanceClockError(f"{path} datetime must be timezone-aware")
        normalized = value.astimezone(timezone.utc)
        if normalized.microsecond:
            raise AcceptanceClockError(f"{path} must have second precision")
        return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")
    raise AcceptanceClockError(f"{path} must be a timestamp string or datetime")


def _system_acceptance_clock(
    capture_request: Mapping[str, Any],
) -> datetime:
    """Default trusted store clock; the request argument supports injection parity."""

    del capture_request
    return datetime.now(timezone.utc).replace(microsecond=0)


def _canonical_url(value: object) -> str:
    text = _bounded_string(value, "source.canonical_url", 2_048)
    if "\\" in text or any(character.isspace() for character in text):
        raise EvidenceDocumentError("source.canonical_url is not canonical")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise EvidenceDocumentError("source.canonical_url is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EvidenceDocumentError(
            "source.canonical_url must be an absolute HTTP(S) URL"
        )
    if not text.startswith(f"{parsed.scheme}://"):
        raise EvidenceDocumentError("source.canonical_url scheme must be lowercase")
    if parsed.username is not None or parsed.password is not None or "#" in text:
        raise EvidenceDocumentError(
            "source.canonical_url cannot contain credentials or a fragment"
        )
    authority = text.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]
    if authority.endswith(":") or text.endswith("?"):
        raise EvidenceDocumentError(
            "source.canonical_url cannot contain an empty port or query delimiter"
        )
    if parsed.netloc != parsed.netloc.lower():
        raise EvidenceDocumentError("source.canonical_url hostname must be lowercase")
    try:
        parsed.hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise EvidenceDocumentError(
            "source.canonical_url hostname must use lowercase ASCII or punycode"
        ) from exc
    if ":" in parsed.hostname:
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise EvidenceDocumentError(
                "source.canonical_url IPv6 host is invalid"
            ) from exc
        if address.version != 6 or f"[{address.compressed}]" not in parsed.netloc:
            raise EvidenceDocumentError(
                "source.canonical_url IPv6 host is not canonical"
            )
    elif not _DNS_HOST_RE.fullmatch(parsed.hostname):
        raise EvidenceDocumentError("source.canonical_url hostname is invalid")
    if (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    ):
        raise EvidenceDocumentError(
            "source.canonical_url must omit the scheme's default port"
        )
    if port is not None:
        if authority.startswith("["):
            closing_bracket = authority.find("]")
            port_text = (
                authority[closing_bracket + 2 :]
                if authority[closing_bracket + 1 :].startswith(":")
                else None
            )
        else:
            port_text = authority.rsplit(":", 1)[1] if ":" in authority else None
        if port_text is not None and port_text != str(port):
            raise EvidenceDocumentError(
                "source.canonical_url port must be canonical decimal without leading zeros"
            )
    if "%" in parsed.hostname:
        raise EvidenceDocumentError(
            "source.canonical_url IPv6 scope identifiers are not supported"
        )
    decoded_path = unquote(parsed.path)
    if any(part in {".", ".."} for part in decoded_path.split("/")):
        raise EvidenceDocumentError(
            "source.canonical_url path cannot contain dot segments"
        )
    index = 0
    while index < len(text):
        if text[index] != "%":
            index += 1
            continue
        escape = text[index + 1 : index + 3]
        if len(escape) != 2 or not re.fullmatch(r"[0-9A-F]{2}", escape):
            raise EvidenceDocumentError(
                "source.canonical_url percent escapes must use two uppercase hex digits"
            )
        index += 3
    return text


def _metadata_document(value: Mapping[str, Any] | bytes | str) -> dict[str, Any]:
    if type(value) in (bytes, str):
        parsed = strict_json_loads(value)
    elif isinstance(value, Mapping):
        # Copy before validation so later caller mutations cannot alter the
        # manifest that was committed.
        parsed = deepcopy(dict(value))
    else:
        raise EvidenceDocumentError("evidence metadata must be an object or JSON")
    return _validate_metadata(parsed)


def _validate_metadata(value: object) -> dict[str, Any]:
    document = _exact_object(value, _METADATA_KEYS, "evidence metadata")

    source = _exact_object(
        document["source"], frozenset({"id", "canonical_url"}), "source"
    )
    source_id = _safe_identifier(source["id"], "source.id")
    canonical_url = _canonical_url(source["canonical_url"])

    media_type = _bounded_string(document["media_type"], "media_type", 127)
    if not _MEDIA_TYPE_RE.fullmatch(media_type):
        raise EvidenceDocumentError(
            "media_type must be a lowercase type/subtype without parameters"
        )
    language = _bounded_string(document["language"], "language", 63)
    if not _LANGUAGE_RE.fullmatch(language):
        raise EvidenceDocumentError(
            "language must be a lowercase BCP-47-style tag or 'und'"
        )

    event_time = _timestamp(document["event_time"], "event_time", nullable=True)
    publication_time = _timestamp(
        document["publication_time"], "publication_time", nullable=True
    )
    knowledge_time = _timestamp(document["knowledge_time"], "knowledge_time")
    collected_at = _timestamp(document["collected_at"], "collected_at")
    assert knowledge_time is not None and collected_at is not None
    knowledge_value = _timestamp_value(knowledge_time)
    # event_time is descriptive and may be future scheduled relative to the
    # publication/knowledge clocks.  It is never an availability cutoff.
    if (
        publication_time is not None
        and _timestamp_value(publication_time) > knowledge_value
    ):
        raise EvidenceDocumentError("publication_time cannot follow knowledge_time")
    if knowledge_value > _timestamp_value(collected_at):
        raise EvidenceDocumentError("knowledge_time cannot follow collected_at")

    collection = _exact_object(
        document["collection"],
        frozenset({"run_id", "parent_feed_sha256"}),
        "collection",
    )
    run_id = _safe_identifier(collection["run_id"], "collection.run_id")
    parent_feed_sha256 = _sha256_value(
        collection["parent_feed_sha256"],
        "collection.parent_feed_sha256",
        nullable=True,
    )

    retention_class = _bounded_string(
        document["retention_class"], "retention_class", 64
    )
    if not _RETENTION_RE.fullmatch(retention_class):
        raise EvidenceDocumentError("retention_class is not a safe class name")

    rights = _exact_object(
        document["rights"],
        frozenset({"training_use", "license_or_terms_ref"}),
        "rights",
    )
    training_use = _bounded_string(rights["training_use"], "rights.training_use", 32)
    if training_use not in TRAINING_USES:
        raise EvidenceDocumentError(
            "rights.training_use must be one of " + ", ".join(sorted(TRAINING_USES))
        )
    license_or_terms_ref = _bounded_string(
        rights["license_or_terms_ref"], "rights.license_or_terms_ref", 2_048
    )

    normalized = {
        "source": {"id": source_id, "canonical_url": canonical_url},
        "media_type": media_type,
        "language": language,
        "event_time": event_time,
        "publication_time": publication_time,
        "knowledge_time": knowledge_time,
        "collected_at": collected_at,
        "collection": {
            "run_id": run_id,
            "parent_feed_sha256": parent_feed_sha256,
        },
        "retention_class": retention_class,
        "rights": {
            "training_use": training_use,
            "license_or_terms_ref": license_or_terms_ref,
        },
    }
    encoded = canonical_json_bytes(normalized)
    if len(encoded) > MAX_METADATA_BYTES:
        raise EvidenceDocumentError(
            f"canonical evidence metadata exceeds {MAX_METADATA_BYTES} bytes"
        )
    return normalized


def _content_record(value: object, path: str = "content") -> dict[str, Any]:
    record = _exact_object(value, _CONTENT_KEYS, path)
    content_sha256 = _sha256_value(record["sha256"], f"{path}.sha256")
    assert content_sha256 is not None
    byte_size = record["byte_size"]
    if type(byte_size) is not int or not (1 <= byte_size <= MAX_DOCUMENT_BYTES):
        raise EvidenceDocumentError(
            f"{path}.byte_size must be an integer from 1 through {MAX_DOCUMENT_BYTES}"
        )
    return {"sha256": content_sha256, "byte_size": byte_size}


def _capture_request_for(
    metadata: Mapping[str, Any], content_sha256: str, byte_size: int
) -> dict[str, Any]:
    return {
        "spec_version": CAPTURE_REQUEST_SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "metadata": deepcopy(dict(metadata)),
        "content": {"sha256": content_sha256, "byte_size": byte_size},
    }


def validate_capture_request(
    value: Mapping[str, Any] | bytes | str,
) -> dict[str, Any]:
    """Validate the deterministic request identity used by acceptance receipts."""

    if type(value) in (bytes, str):
        parsed = strict_json_loads(
            value,
            maximum_bytes=MAX_ACCEPTANCE_RECEIPT_BYTES,
            purpose="capture request",
        )
    elif isinstance(value, Mapping):
        parsed = deepcopy(dict(value))
    else:
        raise EvidenceDocumentError("capture request must be an object or JSON")
    request = _exact_object(parsed, _CAPTURE_REQUEST_KEYS, "capture request")
    if request["spec_version"] != CAPTURE_REQUEST_SPEC_VERSION:
        raise EvidenceDocumentError("unsupported capture request spec_version")
    if request["canonicalization"] != CANONICALIZATION:
        raise EvidenceDocumentError("unsupported capture request canonicalization")
    metadata = _validate_metadata(request["metadata"])
    content = _content_record(request["content"], "capture request.content")
    normalized = _capture_request_for(metadata, content["sha256"], content["byte_size"])
    if len(canonical_json_bytes(normalized)) > MAX_ACCEPTANCE_RECEIPT_BYTES:
        raise EvidenceDocumentError("canonical capture request is too large")
    return normalized


def capture_request_sha256(value: Mapping[str, Any] | bytes | str) -> str:
    return _sha256(canonical_json_bytes(validate_capture_request(value)))


def _acceptance_receipt_for(
    capture_request: Mapping[str, Any], accepted_at: str
) -> dict[str, Any]:
    normalized_request = deepcopy(dict(capture_request))
    return {
        "spec_version": ACCEPTANCE_RECEIPT_SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "capture_request_sha256": _sha256(canonical_json_bytes(normalized_request)),
        "accepted_at": accepted_at,
        "capture_request": normalized_request,
    }


def validate_acceptance_receipt(
    value: Mapping[str, Any] | bytes | str,
) -> dict[str, Any]:
    """Validate a create-once store acceptance receipt and its request binding."""

    if type(value) in (bytes, str):
        parsed = strict_json_loads(
            value,
            maximum_bytes=MAX_ACCEPTANCE_RECEIPT_BYTES,
            purpose="acceptance receipt",
        )
    elif isinstance(value, Mapping):
        parsed = deepcopy(dict(value))
    else:
        raise EvidenceDocumentError("acceptance receipt must be an object or JSON")
    receipt = _exact_object(parsed, _ACCEPTANCE_RECEIPT_KEYS, "acceptance receipt")
    if receipt["spec_version"] != ACCEPTANCE_RECEIPT_SPEC_VERSION:
        raise EvidenceDocumentError("unsupported acceptance receipt spec_version")
    if receipt["canonicalization"] != CANONICALIZATION:
        raise EvidenceDocumentError("unsupported acceptance receipt canonicalization")
    request = validate_capture_request(receipt["capture_request"])
    request_sha256 = _sha256_value(
        receipt["capture_request_sha256"],
        "acceptance receipt.capture_request_sha256",
    )
    assert request_sha256 is not None
    if _sha256(canonical_json_bytes(request)) != request_sha256:
        raise IntegrityError("acceptance receipt does not match its capture request")
    accepted_at = _timestamp(receipt["accepted_at"], "acceptance receipt.accepted_at")
    assert accepted_at is not None
    if _timestamp_value(accepted_at) < _timestamp_value(
        request["metadata"]["collected_at"]
    ):
        raise AcceptanceClockError(
            "acceptance receipt accepted_at cannot precede collected_at"
        )
    normalized = _acceptance_receipt_for(request, accepted_at)
    if len(canonical_json_bytes(normalized)) > MAX_ACCEPTANCE_RECEIPT_BYTES:
        raise EvidenceDocumentError("canonical acceptance receipt is too large")
    return normalized


def _acceptance_binding(
    value: object, *, capture_request_sha256: str
) -> dict[str, Any]:
    binding = _exact_object(value, _ACCEPTANCE_BINDING_KEYS, "acceptance")
    accepted_at = _timestamp(binding["accepted_at"], "acceptance.accepted_at")
    declared_request = _sha256_value(
        binding["capture_request_sha256"], "acceptance.capture_request_sha256"
    )
    receipt_sha256 = _sha256_value(
        binding["receipt_sha256"], "acceptance.receipt_sha256"
    )
    assert accepted_at is not None
    assert declared_request is not None and receipt_sha256 is not None
    if declared_request != capture_request_sha256:
        raise IntegrityError("manifest acceptance does not bind its capture request")
    return {
        "accepted_at": accepted_at,
        "capture_request_sha256": declared_request,
        "receipt_sha256": receipt_sha256,
    }


def _manifest_for(
    metadata: dict[str, Any],
    content_sha256: str,
    byte_size: int,
    acceptance: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "spec_version": SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        **deepcopy(metadata),
        "content": {"sha256": content_sha256, "byte_size": byte_size},
        "acceptance": deepcopy(dict(acceptance)),
    }


def validate_manifest(
    value: Mapping[str, Any] | bytes | str,
    *,
    content: bytes | bytearray | memoryview | None = None,
) -> dict[str, Any]:
    """Validate a stored manifest and optionally bind it to exact content bytes."""

    if type(value) in (bytes, str):
        parsed = strict_json_loads(
            value, maximum_bytes=MAX_MANIFEST_BYTES, purpose="evidence manifest"
        )
    elif isinstance(value, Mapping):
        parsed = deepcopy(dict(value))
    else:
        raise EvidenceDocumentError("evidence manifest must be an object or JSON")
    document = _exact_object(parsed, _MANIFEST_KEYS, "evidence manifest")
    if document["spec_version"] != SPEC_VERSION:
        raise EvidenceDocumentError("unsupported evidence manifest spec_version")
    if document["canonicalization"] != CANONICALIZATION:
        raise EvidenceDocumentError("unsupported manifest canonicalization")

    metadata = _validate_metadata({key: document[key] for key in _METADATA_KEYS})
    content_record = _content_record(document["content"])
    content_sha256 = content_record["sha256"]
    byte_size = content_record["byte_size"]
    request = _capture_request_for(metadata, content_sha256, byte_size)
    request_sha256 = _sha256(canonical_json_bytes(request))
    acceptance = _acceptance_binding(
        document["acceptance"], capture_request_sha256=request_sha256
    )
    if _timestamp_value(acceptance["accepted_at"]) < _timestamp_value(
        metadata["collected_at"]
    ):
        raise AcceptanceClockError("manifest accepted_at cannot precede collected_at")
    normalized = _manifest_for(metadata, content_sha256, byte_size, acceptance)
    encoded = canonical_json_bytes(normalized)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise EvidenceDocumentError(
            f"canonical evidence manifest exceeds {MAX_MANIFEST_BYTES} bytes"
        )

    if content is not None:
        raw = _content_bytes(content)
        if len(raw) != byte_size or _sha256(raw) != content_sha256:
            raise IntegrityError("manifest content digest or byte size does not match")
    return normalized


def validate_rights_decision(
    value: Mapping[str, Any] | bytes | str,
) -> dict[str, Any]:
    """Validate and normalize one immutable rights decision body.

    The decision identity is the SHA-256 of the returned canonical object.  A
    ledger entry carries that identity separately so ``supersedes`` can bind to
    an exact prior decision without a self-referential hash.
    """

    if type(value) in (bytes, str):
        parsed = strict_json_loads(
            value,
            maximum_bytes=MAX_METADATA_BYTES,
            purpose="rights decision",
        )
    elif isinstance(value, Mapping):
        parsed = deepcopy(dict(value))
    else:
        raise EvidenceDocumentError("rights decision must be an object or JSON")
    decision = _exact_object(parsed, _RIGHTS_DECISION_KEYS, "rights decision")
    if decision["spec_version"] != RIGHTS_DECISION_SPEC_VERSION:
        raise EvidenceDocumentError("unsupported rights decision spec_version")
    if decision["canonicalization"] != CANONICALIZATION:
        raise EvidenceDocumentError("unsupported rights decision canonicalization")
    subject = _exact_object(
        decision["subject"],
        frozenset({"source_id", "content_sha256"}),
        "rights decision.subject",
    )
    source_id = _safe_identifier(
        subject["source_id"], "rights decision.subject.source_id"
    )
    content_sha256 = _sha256_value(
        subject["content_sha256"], "rights decision.subject.content_sha256"
    )
    assert content_sha256 is not None
    decision_type = _bounded_string(
        decision["decision_type"], "rights decision.decision_type", 32
    )
    if decision_type not in {"policy_set", "revocation"}:
        raise EvidenceDocumentError(
            "rights decision.decision_type must be policy_set or revocation"
        )
    training_use = _bounded_string(
        decision["training_use"], "rights decision.training_use", 32
    )
    if training_use not in TRAINING_USES:
        raise EvidenceDocumentError(
            "rights decision.training_use must be one of "
            + ", ".join(sorted(TRAINING_USES))
        )
    if decision_type == "revocation" and training_use != "prohibited":
        raise EvidenceDocumentError(
            "a rights revocation must set training_use to prohibited"
        )
    effective_at = _timestamp(decision["effective_at"], "rights decision.effective_at")
    knowledge_time = _timestamp(
        decision["knowledge_time"], "rights decision.knowledge_time"
    )
    assert effective_at is not None and knowledge_time is not None
    license_or_terms_ref = _bounded_string(
        decision["license_or_terms_ref"],
        "rights decision.license_or_terms_ref",
        2_048,
    )
    reason = _bounded_string(decision["reason"], "rights decision.reason", 1_024)
    supersedes_value = decision["supersedes"]
    if type(supersedes_value) is not list:
        raise EvidenceDocumentError("rights decision.supersedes must be an array")
    if len(supersedes_value) > MAX_SUPERSEDES:
        raise EvidenceDocumentError(
            f"rights decision.supersedes exceeds {MAX_SUPERSEDES} entries"
        )
    supersedes: list[str] = []
    for index, digest in enumerate(supersedes_value):
        parsed_digest = _sha256_value(digest, f"rights decision.supersedes[{index}]")
        assert parsed_digest is not None
        supersedes.append(parsed_digest)
    if len(set(supersedes)) != len(supersedes):
        raise EvidenceDocumentError("rights decision.supersedes contains duplicates")

    normalized = {
        "spec_version": RIGHTS_DECISION_SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "subject": {
            "source_id": source_id,
            "content_sha256": content_sha256,
        },
        "decision_type": decision_type,
        "training_use": training_use,
        "effective_at": effective_at,
        "knowledge_time": knowledge_time,
        "license_or_terms_ref": license_or_terms_ref,
        "reason": reason,
        "supersedes": sorted(supersedes),
    }
    encoded = canonical_json_bytes(normalized)
    if len(encoded) > MAX_METADATA_BYTES:
        raise EvidenceDocumentError(
            f"canonical rights decision exceeds {MAX_METADATA_BYTES} bytes"
        )
    return normalized


def rights_decision_sha256(value: Mapping[str, Any] | bytes | str) -> str:
    """Return the immutable identity of a validated rights decision body."""

    return _sha256(canonical_json_bytes(validate_rights_decision(value)))


def make_rights_decision_entry(
    value: Mapping[str, Any] | bytes | str,
) -> dict[str, Any]:
    """Create a hash-bound ledger entry from one decision body."""

    decision = validate_rights_decision(value)
    return {
        "decision_sha256": _sha256(canonical_json_bytes(decision)),
        "decision": decision,
    }


def validate_rights_ledger(
    value: Mapping[str, Any] | bytes | str | None,
) -> dict[str, Any]:
    """Validate a complete bounded decision ledger and its supersession graph."""

    if value is None:
        parsed: object = {
            "spec_version": RIGHTS_LEDGER_SPEC_VERSION,
            "canonicalization": CANONICALIZATION,
            "decisions": [],
        }
    elif type(value) in (bytes, str):
        parsed = strict_json_loads(
            value,
            maximum_bytes=MAX_RIGHTS_LEDGER_BYTES,
            purpose="rights ledger",
        )
    elif isinstance(value, Mapping):
        parsed = deepcopy(dict(value))
    else:
        raise EvidenceDocumentError("rights ledger must be an object or JSON")
    ledger = _exact_object(parsed, _RIGHTS_LEDGER_KEYS, "rights ledger")
    if ledger["spec_version"] != RIGHTS_LEDGER_SPEC_VERSION:
        raise EvidenceDocumentError("unsupported rights ledger spec_version")
    if ledger["canonicalization"] != CANONICALIZATION:
        raise EvidenceDocumentError("unsupported rights ledger canonicalization")
    raw_entries = ledger["decisions"]
    if type(raw_entries) is not list:
        raise EvidenceDocumentError("rights ledger.decisions must be an array")
    if len(raw_entries) > MAX_RIGHTS_DECISIONS:
        raise EvidenceDocumentError(
            f"rights ledger exceeds {MAX_RIGHTS_DECISIONS} decisions"
        )

    entries: list[dict[str, Any]] = []
    by_digest: dict[str, dict[str, Any]] = {}
    for index, raw_entry in enumerate(raw_entries):
        entry = _exact_object(
            raw_entry, _RIGHTS_ENTRY_KEYS, f"rights ledger.decisions[{index}]"
        )
        declared_digest = _sha256_value(
            entry["decision_sha256"],
            f"rights ledger.decisions[{index}].decision_sha256",
        )
        assert declared_digest is not None
        decision = validate_rights_decision(entry["decision"])
        actual_digest = _sha256(canonical_json_bytes(decision))
        if actual_digest != declared_digest:
            raise IntegrityError(
                f"rights decision {index} does not match decision_sha256"
            )
        if declared_digest in by_digest:
            raise EvidenceDocumentError(f"duplicate rights decision {declared_digest}")
        normalized_entry = {
            "decision_sha256": declared_digest,
            "decision": decision,
        }
        entries.append(normalized_entry)
        by_digest[declared_digest] = normalized_entry

    for digest, entry in by_digest.items():
        decision = entry["decision"]
        subject = decision["subject"]
        decision_knowledge = _timestamp_value(decision["knowledge_time"])
        for superseded_digest in decision["supersedes"]:
            if superseded_digest == digest:
                raise EvidenceDocumentError("rights decision cannot supersede itself")
            superseded = by_digest.get(superseded_digest)
            if superseded is None:
                raise EvidenceDocumentError(
                    f"rights decision supersedes unknown decision {superseded_digest}"
                )
            prior = superseded["decision"]
            if prior["subject"] != subject:
                raise EvidenceDocumentError(
                    "rights decision cannot supersede a different subject"
                )
            if decision_knowledge < _timestamp_value(prior["knowledge_time"]):
                raise EvidenceDocumentError(
                    "superseding decision knowledge_time cannot precede prior knowledge_time"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(digest: str) -> None:
        if digest in visited:
            return
        if digest in visiting:
            raise EvidenceDocumentError("rights decision supersession graph is cyclic")
        visiting.add(digest)
        for prior_digest in by_digest[digest]["decision"]["supersedes"]:
            visit(prior_digest)
        visiting.remove(digest)
        visited.add(digest)

    for digest in by_digest:
        visit(digest)

    entries.sort(
        key=lambda entry: (
            entry["decision"]["subject"]["source_id"],
            entry["decision"]["subject"]["content_sha256"],
            entry["decision"]["knowledge_time"],
            entry["decision"]["effective_at"],
            entry["decision_sha256"],
        )
    )
    normalized = {
        "spec_version": RIGHTS_LEDGER_SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "decisions": entries,
    }
    encoded = canonical_json_bytes(normalized)
    if len(encoded) > MAX_RIGHTS_LEDGER_BYTES:
        raise EvidenceDocumentError(
            f"canonical rights ledger exceeds {MAX_RIGHTS_LEDGER_BYTES} bytes"
        )
    return normalized


def empty_rights_ledger() -> dict[str, Any]:
    """Return a fresh, explicit empty v1 rights ledger."""

    return validate_rights_ledger(None)


def rights_ledger_sha256(
    value: Mapping[str, Any] | bytes | str | None,
) -> str:
    """Hash a complete normalized ledger for out-of-band head anchoring."""

    return _sha256(canonical_json_bytes(validate_rights_ledger(value)))


def _rights_ledger_as_of(ledger: Mapping[str, Any], cutoff: datetime) -> dict[str, Any]:
    """Return the complete ledger visible at ``cutoff``, excluding future knowledge."""

    visible = [
        deepcopy(entry)
        for entry in ledger["decisions"]
        if _timestamp_value(entry["decision"]["knowledge_time"]) <= cutoff
    ]
    return {
        "spec_version": RIGHTS_LEDGER_SPEC_VERSION,
        "canonicalization": CANONICALIZATION,
        "decisions": visible,
    }


def _resolve_effective_rights(
    *,
    source_id: str,
    content_sha256: str,
    manifests: list[tuple[str, Mapping[str, Any]]],
    visible_ledger: Mapping[str, Any],
    cutoff: datetime,
) -> dict[str, Any] | None:
    subject_entries = [
        entry
        for entry in visible_ledger["decisions"]
        if entry["decision"]["subject"]
        == {"source_id": source_id, "content_sha256": content_sha256}
        and _timestamp_value(entry["decision"]["effective_at"]) <= cutoff
    ]
    superseded = {
        digest
        for entry in subject_entries
        for digest in entry["decision"]["supersedes"]
    }
    terminals = [
        entry for entry in subject_entries if entry["decision_sha256"] not in superseded
    ]
    if len(terminals) > 1:
        terminal_ids = sorted(entry["decision_sha256"] for entry in terminals)
        raise RightsConflictError(
            "unresolved terminal rights decisions for "
            f"{source_id}/{content_sha256}: {terminal_ids}"
        )
    manifest_ids = sorted(manifest_sha256 for manifest_sha256, _ in manifests)
    if not terminals:
        return None
    terminal = terminals[0]
    decision = terminal["decision"]
    return {
        "training_use": decision["training_use"],
        "license_or_terms_ref": decision["license_or_terms_ref"],
        "basis": {
            "type": "rights_decision",
            "decision_sha256s": [terminal["decision_sha256"]],
            "manifest_sha256s": manifest_ids,
        },
    }


def _content_bytes(value: bytes | bytearray | memoryview) -> bytes:
    if type(value) is bytes:
        return value
    if type(value) in (bytearray, memoryview):
        return bytes(value)
    raise EvidenceDocumentError("content must be caller-supplied bytes")


def _validate_root_argument(value: str | os.PathLike[str]) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise StoreSafetyError(
            "store_root must be an absolute filesystem path"
        ) from exc
    if type(raw) is bytes:
        try:
            raw = os.fsdecode(raw)
        except UnicodeError as exc:
            raise StoreSafetyError("store_root is not a valid filesystem path") from exc
    if type(raw) is not str or not raw or "\x00" in raw:
        raise StoreSafetyError("store_root is not a valid filesystem path")
    requested_root = Path(raw)
    if not requested_root.is_absolute():
        raise StoreSafetyError("store_root must be an absolute path")
    if any(component == ".." for component in requested_root.parts):
        raise StoreSafetyError("store_root cannot contain path traversal")
    # The explicit path is the security boundary.  Do not silently resolve away
    # an existing symlink in any component; callers on platforms with a
    # symlinked convenience prefix must pass the canonical spelling.
    _assert_directory_path_without_symlinks(requested_root, allow_missing=True)
    _assert_trusted_ancestor_chain(requested_root.parent)
    root = requested_root
    anchor = Path(root.anchor)
    if root == anchor:
        raise StoreSafetyError("filesystem root cannot be an evidence store")
    return root


def _assert_trusted_ancestor_chain(path: Path) -> None:
    """Require every existing store ancestor to resist untrusted replacement.

    Path-based creation and reopening are safe only while an untrusted identity
    cannot rename an intermediate component.  The effective UID and root are
    the v1 trust principals.  A sticky directory owned by either principal is
    also acceptable (for example ``/tmp``), because another UID cannot replace
    the caller-owned child entry there.
    """

    current = Path(path.anchor)
    trusted_uids = {0, os.geteuid()}
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StoreSafetyError(
                f"cannot inspect store ancestor {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            # The no-symlink directory walk normally reports this first.  Keep
            # this helper independently fail-closed for direct future callers.
            raise StoreSafetyError(f"store ancestor is not a real directory: {current}")
        if metadata.st_uid not in trusted_uids:
            raise StoreSafetyError(
                f"store ancestor is owned by an untrusted UID: {current}"
            )
        writable_by_others = stat.S_IMODE(metadata.st_mode) & 0o022
        sticky = bool(metadata.st_mode & stat.S_ISVTX)
        if writable_by_others and not sticky:
            raise StoreSafetyError(
                "store ancestor writable by group or other must be sticky: "
                f"{current}"
            )


def _assert_directory_path_without_symlinks(path: Path, *, allow_missing: bool) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                return
            raise StoreSafetyError(f"store directory does not exist: {current}")
        except OSError as exc:
            raise StoreSafetyError(
                f"cannot inspect store path {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise StoreSafetyError(f"symlinks are forbidden in store path: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise StoreSafetyError(
                f"store path component is not a directory: {current}"
            )


def _assert_private_directory(path: Path, purpose: str) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise StoreSafetyError(f"cannot inspect {purpose} {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise StoreSafetyError(f"symlink {purpose} is forbidden: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise StoreSafetyError(f"{purpose} is not a directory: {path}")
    if metadata.st_uid != os.geteuid():
        raise StoreSafetyError(f"{purpose} must be owned by the effective user: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o700:
        raise StoreSafetyError(f"{purpose} must have mode 0700, got {mode:04o}: {path}")
    return metadata


def _read_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    purpose: str,
    expected_nlink: int = 1,
) -> bytes:
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        raise IntegrityError(f"{purpose} is missing: {path}") from exc
    except OSError as exc:
        raise IntegrityError(f"cannot inspect {purpose}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise StoreSafetyError(f"symlink {purpose} is forbidden: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise IntegrityError(f"{purpose} is not a regular file: {path}")
    if before.st_uid != os.geteuid():
        raise StoreSafetyError(f"{purpose} must be owned by the effective user: {path}")
    mode = stat.S_IMODE(before.st_mode)
    if mode != 0o600:
        raise StoreSafetyError(f"{purpose} must have mode 0600, got {mode:04o}: {path}")
    if before.st_nlink != expected_nlink:
        raise StoreSafetyError(
            f"{purpose} must have exactly {expected_nlink} hard link(s), "
            f"got {before.st_nlink}: {path}"
        )
    if before.st_size > maximum_bytes:
        raise IntegrityError(f"{purpose} exceeds the {maximum_bytes}-byte limit")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StoreSafetyError(f"symlink {purpose} is forbidden: {path}") from exc
        raise IntegrityError(f"cannot open {purpose}: {exc}") from exc
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode):
            raise IntegrityError(f"{purpose} is not a regular file: {path}")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise IntegrityError(f"{purpose} changed while it was opened")
        if after.st_nlink != expected_nlink:
            raise StoreSafetyError(f"{purpose} acquired an unexpected hard link")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise IntegrityError(f"{purpose} exceeds the {maximum_bytes}-byte limit")
        final = os.fstat(descriptor)
        if (
            final.st_size != len(payload)
            or final.st_size != after.st_size
            or final.st_mtime_ns != after.st_mtime_ns
            or final.st_nlink != expected_nlink
        ):
            raise IntegrityError(f"{purpose} changed while it was read")
        return payload
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise DurabilityError(
            f"cannot open directory for fsync {directory}: {exc}"
        ) from exc
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise DurabilityError(
                f"filesystem cannot durably fsync directory {directory}: {exc}"
            ) from exc
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path, purpose: str) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DurabilityError(f"cannot open {purpose} for fsync: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise IntegrityError(f"{purpose} is not a regular file: {path}")
        os.fsync(descriptor)
    except OSError as exc:
        raise DurabilityError(f"cannot fsync {purpose}: {exc}") from exc
    finally:
        os.close(descriptor)


def _open_stable_directory(path: Path, purpose: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = os.lstat(path)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StoreSafetyError(
            f"cannot open stable {purpose} directory: {exc}"
        ) from exc
    after = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        os.close(descriptor)
        raise StoreSafetyError(f"{purpose} directory changed while it was opened")
    return descriptor, after


def _fsync_descriptor(descriptor: int, purpose: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise DurabilityError(
            f"cannot fsync anchored {purpose} directory: {exc}"
        ) from exc


def _after_hard_link_for_testing(temporary: Path, destination: Path) -> None:
    """No-op failure-injection seam used by subprocess crash tests."""

    del temporary, destination


def _commit_temp_file(
    temporary: Path,
    destination: Path,
    *,
    payload_sha256: str,
    maximum_bytes: int,
    purpose: str,
) -> None:
    """Atomically create and durably sync ``destination`` using anchored dirfds.

    The staged payload is verified before linking.  The destination directory
    is fsynced *before* the staging alias is removed, making every crash state
    distinguishable: an unfinalized destination still has the bound staging
    alias and a finalized one has a durable destination link.  On an ordinary
    handled failure the destination is removed through its anchored descriptor.
    """

    if (
        os.name != "posix"
        or os.link not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
    ):
        raise HardLinkUnsupportedError(
            "EvidenceDocument v1 requires POSIX hard links with dir_fd support"
        )
    source_fd, source_directory = _open_stable_directory(temporary.parent, "staging")
    destination_fd, destination_directory = _open_stable_directory(
        destination.parent, "destination"
    )
    linked = False
    try:
        if source_directory.st_dev != destination_directory.st_dev:
            raise HardLinkUnsupportedError(
                "staging and destination must be on the same hard-link filesystem"
            )
        try:
            source_file = os.stat(
                temporary.name, dir_fd=source_fd, follow_symlinks=False
            )
        except OSError as exc:
            raise StoreSafetyError(f"cannot inspect staged file: {exc}") from exc
        path_file = os.lstat(temporary)
        if (
            not stat.S_ISREG(source_file.st_mode)
            or source_file.st_uid != os.geteuid()
            or stat.S_IMODE(source_file.st_mode) != 0o600
            or source_file.st_nlink != 1
            or (source_file.st_dev, source_file.st_ino)
            != (path_file.st_dev, path_file.st_ino)
        ):
            raise StoreSafetyError("staged file changed before commit")
        staged_payload = _read_regular_file(
            temporary,
            maximum_bytes=maximum_bytes,
            purpose=f"staged {purpose}",
        )
        if _sha256(staged_payload) != payload_sha256:
            raise IntegrityError(f"staged {purpose} does not match its intent digest")
        try:
            os.link(
                temporary.name,
                destination.name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            linked = True
            _after_hard_link_for_testing(temporary, destination)
        except FileExistsError:
            raise
        except (NotImplementedError, OSError) as exc:
            unsupported = isinstance(exc, NotImplementedError) or getattr(
                exc, "errno", None
            ) in {
                errno.EXDEV,
                errno.ENOSYS,
                getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                errno.EOPNOTSUPP,
            }
            if unsupported:
                raise HardLinkUnsupportedError(
                    f"filesystem does not support required atomic hard links: {exc}"
                ) from exc
            raise StoreSafetyError(f"atomic hard-link commit failed: {exc}") from exc

        try:
            current_parent = os.lstat(destination.parent)
            current_staging = os.lstat(temporary.parent)
            if (current_parent.st_dev, current_parent.st_ino) != (
                destination_directory.st_dev,
                destination_directory.st_ino,
            ) or (current_staging.st_dev, current_staging.st_ino) != (
                source_directory.st_dev,
                source_directory.st_ino,
            ):
                raise StoreSafetyError(
                    "staging or destination directory changed during atomic commit"
                )
            committed = os.stat(
                destination.name, dir_fd=destination_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(committed.st_mode)
                or committed.st_uid != os.geteuid()
                or stat.S_IMODE(committed.st_mode) != 0o600
                or committed.st_nlink != 2
                or (committed.st_dev, committed.st_ino)
                != (source_file.st_dev, source_file.st_ino)
            ):
                raise StoreSafetyError(
                    "committed object failed private ownership/mode/link checks"
                )
            # The destination acceptance link is durable before its recovery
            # alias is removed.  A crash before the unlink is recoverable; a
            # crash after it cannot expose a pre-fsync manifest.
            _fsync_descriptor(destination_fd, "destination")
            os.unlink(temporary.name, dir_fd=source_fd)
            _fsync_descriptor(source_fd, "staging")
            finalized = os.stat(
                destination.name, dir_fd=destination_fd, follow_symlinks=False
            )
            if finalized.st_nlink != 1 or (
                finalized.st_dev,
                finalized.st_ino,
            ) != (committed.st_dev, committed.st_ino):
                raise StoreSafetyError("committed object did not finalize to one link")
        except Exception:
            if linked:
                try:
                    os.unlink(destination.name, dir_fd=destination_fd)
                except FileNotFoundError:
                    pass
                try:
                    os.fsync(destination_fd)
                except OSError:
                    pass
            raise
    finally:
        os.close(source_fd)
        os.close(destination_fd)


def _finalize_staging_alias(
    temporary: Path,
    destination: Path,
    *,
    payload_sha256: str,
    maximum_bytes: int,
    purpose: str,
) -> None:
    """Durably finish the exact link-before-unlink crash state.

    Only a verified staging alias of the destination inode is unlinked.  Any
    mismatched path, digest, owner, mode, or link count fails closed.
    """

    source_fd, source_directory = _open_stable_directory(temporary.parent, "staging")
    destination_fd, destination_directory = _open_stable_directory(
        destination.parent, "destination"
    )
    try:
        if source_directory.st_dev != destination_directory.st_dev:
            raise StoreSafetyError("recovery alias and destination changed filesystems")
        source = os.stat(temporary.name, dir_fd=source_fd, follow_symlinks=False)
        target = os.stat(destination.name, dir_fd=destination_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(source.st_mode)
            or not stat.S_ISREG(target.st_mode)
            or source.st_uid != os.geteuid()
            or target.st_uid != os.geteuid()
            or stat.S_IMODE(source.st_mode) != 0o600
            or stat.S_IMODE(target.st_mode) != 0o600
            or source.st_nlink != 2
            or target.st_nlink != 2
            or (source.st_dev, source.st_ino) != (target.st_dev, target.st_ino)
        ):
            raise StoreSafetyError("staging recovery state is not one verified alias")
        payload = _read_regular_file(
            temporary,
            maximum_bytes=maximum_bytes,
            purpose=f"recoverable staged {purpose}",
            expected_nlink=2,
        )
        if _sha256(payload) != payload_sha256:
            raise IntegrityError(
                f"recoverable staged {purpose} does not match its intent digest"
            )
        current_parent = os.lstat(destination.parent)
        current_staging = os.lstat(temporary.parent)
        if (current_parent.st_dev, current_parent.st_ino) != (
            destination_directory.st_dev,
            destination_directory.st_ino,
        ) or (current_staging.st_dev, current_staging.st_ino) != (
            source_directory.st_dev,
            source_directory.st_ino,
        ):
            raise StoreSafetyError("staging recovery directory changed")
        _fsync_descriptor(destination_fd, "recovered destination")
        os.unlink(temporary.name, dir_fd=source_fd)
        _fsync_descriptor(source_fd, "recovered staging")
        finalized = os.stat(
            destination.name, dir_fd=destination_fd, follow_symlinks=False
        )
        if finalized.st_nlink != 1 or (
            finalized.st_dev,
            finalized.st_ino,
        ) != (target.st_dev, target.st_ino):
            raise StoreSafetyError("recovered destination did not finalize")
    finally:
        os.close(source_fd)
        os.close(destination_fd)


class EvidenceDocumentStore:
    """An explicit, private, transaction-locked EvidenceDocument v1 store."""

    def __init__(
        self,
        store_root: str | os.PathLike[str],
        *,
        max_document_bytes: int = MAX_DOCUMENT_BYTES,
        acceptance_clock: Callable[[Mapping[str, Any]], str | datetime] | None = None,
    ) -> None:
        if os.name != "posix" or not hasattr(os, "geteuid"):
            raise HardLinkUnsupportedError(
                "EvidenceDocument v1 requires a POSIX filesystem and hard links"
            )
        self.root = _validate_root_argument(store_root)
        if (
            type(max_document_bytes) is not int
            or max_document_bytes < 1
            or max_document_bytes > MAX_DOCUMENT_BYTES
        ):
            raise EvidenceDocumentError(
                f"max_document_bytes must be from 1 through {MAX_DOCUMENT_BYTES}"
            )
        if acceptance_clock is not None and not callable(acceptance_clock):
            raise EvidenceDocumentError("acceptance_clock must be callable")
        self.max_document_bytes = max_document_bytes
        self._acceptance_clock = acceptance_clock or _system_acceptance_clock
        # A recovery worker can drain a large flat staging directory without
        # rescanning its first entries on every bounded call.  The iterator is
        # process-local; restart safely begins a new pass.
        self._recovery_iterator: Any | None = None
        if os.path.lexists(self.root):
            _assert_private_directory(self.root, "existing store_root")

    @property
    def objects_root(self) -> Path:
        return self.root / "objects" / "sha256"

    @property
    def receipts_root(self) -> Path:
        return self.root / "receipts" / "capture-sha256"

    @property
    def manifests_root(self) -> Path:
        return self.root / "manifests" / "sha256"

    @property
    def staging_root(self) -> Path:
        return self.root / ".staging"

    def _assert_private_tree(self, directory: Path) -> None:
        relative = directory.relative_to(self.root)
        current = self.root
        _assert_private_directory(current, "store directory")
        for component in relative.parts:
            current = current / component
            _assert_private_directory(current, "store directory")

    def _ensure_directory(self, directory: Path) -> None:
        try:
            directory.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - fixed internal paths
            raise StoreSafetyError("internal store path escaped store_root") from exc
        _assert_directory_path_without_symlinks(directory, allow_missing=True)
        missing: list[Path] = []
        candidate = directory
        while not os.path.lexists(candidate):
            missing.append(candidate)
            candidate = candidate.parent
        for candidate in reversed(missing):
            try:
                os.mkdir(candidate, 0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise StoreSafetyError(
                    f"cannot create store directory {candidate}: {exc}"
                ) from exc
            _assert_directory_path_without_symlinks(candidate, allow_missing=False)
            _assert_private_directory(candidate, "new store directory")
            _fsync_directory(candidate)
            _fsync_directory(candidate.parent)
        _assert_directory_path_without_symlinks(directory, allow_missing=False)
        self._assert_private_tree(directory)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)

    def _ensure_layout(self) -> None:
        self._ensure_directory(self.staging_root)
        self._ensure_directory(self.objects_root)
        self._ensure_directory(self.receipts_root)
        self._ensure_directory(self.manifests_root)

    @contextmanager
    def _store_guard(self, *, exclusive: bool):
        """Hold the store-wide POSIX transaction barrier."""

        if fcntl is None:  # pragma: no cover - constructor rejects non-POSIX
            raise HardLinkUnsupportedError("POSIX flock is required for the store")
        lock_path = self.staging_root / ".recovery.lock"
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        created = False
        try:
            descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(lock_path, flags)
            except OSError as exc:
                raise StoreSafetyError(
                    f"cannot open existing store transaction lock: {exc}"
                ) from exc
        except OSError as exc:
            raise StoreSafetyError(
                f"cannot open store transaction lock: {exc}"
            ) from exc
        try:
            if created:
                os.fchmod(descriptor, 0o600)
            try:
                os.fsync(descriptor)
            except OSError as exc:
                raise DurabilityError(
                    f"cannot fsync store transaction lock: {exc}"
                ) from exc
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise StoreSafetyError("store transaction lock is not private")
            if created:
                _fsync_directory(self.staging_root)
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(descriptor, operation)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    # Backward-compatible private alias used only by older focused tests.
    _staging_guard = _store_guard

    def content_path(self, content_sha256: str) -> Path:
        digest = _sha256_value(content_sha256, "content_sha256")
        assert digest is not None
        return self.objects_root / digest[:2] / f"{digest}.bin"

    def receipt_path(
        self, capture_request_sha256_value: str, receipt_sha256: str
    ) -> Path:
        request_digest = _sha256_value(
            capture_request_sha256_value, "capture_request_sha256"
        )
        receipt_digest = _sha256_value(receipt_sha256, "receipt_sha256")
        assert request_digest is not None and receipt_digest is not None
        return (
            self.receipts_root
            / request_digest[:2]
            / request_digest
            / f"{receipt_digest}.json"
        )

    def manifest_path(self, manifest_sha256: str) -> Path:
        digest = _sha256_value(manifest_sha256, "manifest_sha256")
        assert digest is not None
        return self.manifests_root / digest[:2] / f"{digest}.json"

    def _intent_path(
        self, kind: str, destination_key: str, payload_sha256: str
    ) -> Path:
        if kind not in {"content", "receipt", "manifest"}:
            raise StoreSafetyError(f"unsupported staging intent kind: {kind}")
        destination_digest = _sha256_value(destination_key, "staging destination key")
        payload_digest = _sha256_value(payload_sha256, "staging payload digest")
        assert destination_digest is not None and payload_digest is not None
        return self.staging_root / (
            f".intent-{kind}-{destination_digest}-{payload_digest}.tmp"
        )

    def _intent_limit(self, kind: str) -> int:
        return {
            "content": self.max_document_bytes,
            "receipt": MAX_ACCEPTANCE_RECEIPT_BYTES,
            "manifest": MAX_MANIFEST_BYTES,
        }[kind]

    def _intent_purpose(self, kind: str) -> str:
        return {
            "content": "content object",
            "receipt": "acceptance receipt",
            "manifest": "manifest",
        }[kind]

    def _destination_for_intent(
        self,
        kind: str,
        destination_key: str,
        payload_sha256: str | None = None,
    ) -> Path:
        if kind == "content":
            return self.content_path(destination_key)
        if kind == "receipt":
            if payload_sha256 is None:
                raise StoreSafetyError("receipt intent lacks its payload digest")
            return self.receipt_path(destination_key, payload_sha256)
        if kind == "manifest":
            return self.manifest_path(destination_key)
        raise StoreSafetyError(f"unsupported staging intent kind: {kind}")

    def _parse_intent(self, path: Path) -> tuple[str, str, str]:
        match = _STAGING_INTENT_RE.fullmatch(path.name)
        if match is None:
            raise StoreSafetyError(f"unexpected staging intent: {path}")
        kind, destination_key, payload_sha256 = match.groups()
        if kind in {"content", "manifest"} and destination_key != payload_sha256:
            raise IntegrityError(f"{kind} staging intent key/digest mismatch")
        return kind, destination_key, payload_sha256

    def _intent_candidates_locked(self, kind: str, destination_key: str) -> list[Path]:
        prefix = f".intent-{kind}-{destination_key}-"
        candidates: list[Path] = []
        inspected = 0
        try:
            with os.scandir(self.staging_root) as iterator:
                for entry in iterator:
                    if entry.name == ".recovery.lock":
                        continue
                    if inspected >= MAX_STAGING_INSPECTIONS:
                        raise EvidenceDocumentError(
                            "staging intent lookup exceeded its bounded inspection "
                            "budget; run recover_staging in batches"
                        )
                    inspected += 1
                    if entry.name.startswith(prefix):
                        candidates.append(Path(entry.path))
                        if len(candidates) > 2:
                            raise StoreSafetyError(
                                "multiple staging intents target one destination"
                            )
        except OSError as exc:
            raise StoreSafetyError(f"cannot scan staging intents: {exc}") from exc
        return sorted(candidates)

    def _verify_recoverable_alias_locked(
        self, kind: str, destination_key: str, destination: Path
    ) -> tuple[Path, str] | None:
        try:
            target = os.lstat(destination)
        except FileNotFoundError:
            return None
        if target.st_nlink != 2:
            return None
        if kind == "receipt":
            match = _RECEIPT_FILENAME_RE.fullmatch(destination.name)
            if match is None or destination.parent.name != destination_key:
                raise StoreSafetyError(
                    "two-link receipt destination does not match its request"
                )
            payload_sha256 = match.group(1)
        else:
            payload_sha256 = destination_key
        candidate = self._intent_path(kind, destination_key, payload_sha256)
        try:
            staged = os.lstat(candidate)
        except FileNotFoundError as exc:
            raise StoreSafetyError(
                "two-link destination lacks its exact bound staging intent"
            ) from exc
        except OSError as exc:
            raise StoreSafetyError(f"cannot inspect staging alias: {exc}") from exc
        if (staged.st_dev, staged.st_ino) != (target.st_dev, target.st_ino):
            raise StoreSafetyError(
                "two-link destination does not share its bound staging inode"
            )
        payload = _read_regular_file(
            candidate,
            maximum_bytes=self._intent_limit(kind),
            purpose=f"recoverable staged {self._intent_purpose(kind)}",
            expected_nlink=2,
        )
        if _sha256(payload) != payload_sha256:
            raise IntegrityError("recoverable staging alias digest mismatch")
        if (
            not stat.S_ISREG(target.st_mode)
            or target.st_uid != os.geteuid()
            or stat.S_IMODE(target.st_mode) != 0o600
        ):
            raise StoreSafetyError("recoverable destination is not private")
        return candidate, payload_sha256

    def _remove_verified_stage_locked(
        self,
        path: Path,
        *,
        payload_sha256: str,
        maximum_bytes: int,
        purpose: str,
    ) -> None:
        payload = _read_regular_file(
            path, maximum_bytes=maximum_bytes, purpose=f"stale staged {purpose}"
        )
        if _sha256(payload) != payload_sha256:
            raise IntegrityError(f"stale staged {purpose} digest mismatch")
        path.unlink()
        _fsync_directory(self.staging_root)

    def _prepare_stage_locked(
        self,
        *,
        kind: str,
        destination_key: str,
        payload: bytes,
        payload_sha256: str,
    ) -> Path:
        purpose = self._intent_purpose(kind)
        path = self._intent_path(kind, destination_key, payload_sha256)
        if os.path.lexists(path):
            existing = _read_regular_file(
                path, maximum_bytes=len(payload), purpose=f"staged {purpose}"
            )
            if existing != payload:
                raise IntegrityError(f"staging intent collision for {purpose}")
            _fsync_regular_file(path, f"staged {purpose}")
            _fsync_directory(self.staging_root)
            return path
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise StoreSafetyError(f"cannot create staged {purpose}: {exc}") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                os.fchmod(handle.fileno(), 0o600)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError as exc:
                    raise DurabilityError(
                        f"cannot fsync staged {purpose}: {exc}"
                    ) from exc
            _fsync_directory(self.staging_root)
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            else:
                _fsync_directory(self.staging_root)
            raise
        return path

    def _install_immutable_locked(
        self,
        destination: Path,
        payload: bytes,
        purpose: str,
        *,
        kind: str,
        destination_key: str,
    ) -> bool:
        self._ensure_directory(destination.parent)
        payload_sha256 = _sha256(payload)
        expected_destination = self._destination_for_intent(
            kind, destination_key, payload_sha256
        )
        if destination != expected_destination:
            raise StoreSafetyError("staging intent destination binding mismatch")
        if kind in {"content", "manifest"} and destination_key != payload_sha256:
            raise IntegrityError(f"immutable {purpose} key does not match payload")
        intent = self._intent_path(kind, destination_key, payload_sha256)

        if os.path.lexists(destination):
            target = os.lstat(destination)
            if target.st_nlink == 2:
                recoverable = self._verify_recoverable_alias_locked(
                    kind, destination_key, destination
                )
                if recoverable is None or recoverable != (intent, payload_sha256):
                    raise IntegrityError(
                        f"immutable {purpose} recovery intent does not match retry"
                    )
                _finalize_staging_alias(
                    intent,
                    destination,
                    payload_sha256=payload_sha256,
                    maximum_bytes=len(payload),
                    purpose=purpose,
                )
            existing = _read_regular_file(
                destination, maximum_bytes=len(payload), purpose=purpose
            )
            if existing != payload:
                raise IntegrityError(
                    f"immutable {purpose} collision or mismatch at {destination}"
                )
            if os.path.lexists(intent):
                self._remove_verified_stage_locked(
                    intent,
                    payload_sha256=payload_sha256,
                    maximum_bytes=len(payload),
                    purpose=purpose,
                )
            _fsync_regular_file(destination, purpose)
            _fsync_directory(destination.parent)
            return False

        temporary = self._prepare_stage_locked(
            kind=kind,
            destination_key=destination_key,
            payload=payload,
            payload_sha256=payload_sha256,
        )
        try:
            _commit_temp_file(
                temporary,
                destination,
                payload_sha256=payload_sha256,
                maximum_bytes=len(payload),
                purpose=purpose,
            )
        except FileExistsError:
            existing = _read_regular_file(
                destination, maximum_bytes=len(payload), purpose=purpose
            )
            if existing != payload:
                raise IntegrityError(
                    f"immutable {purpose} collision or mismatch at {destination}"
                )
            if os.path.lexists(temporary):
                self._remove_verified_stage_locked(
                    temporary,
                    payload_sha256=payload_sha256,
                    maximum_bytes=len(payload),
                    purpose=purpose,
                )
            _fsync_regular_file(destination, purpose)
            _fsync_directory(destination.parent)
            return False
        except (HardLinkUnsupportedError, StoreSafetyError, IntegrityError):
            if os.path.lexists(temporary):
                try:
                    staged = os.lstat(temporary)
                    if staged.st_nlink == 1:
                        self._remove_verified_stage_locked(
                            temporary,
                            payload_sha256=payload_sha256,
                            maximum_bytes=len(payload),
                            purpose=purpose,
                        )
                except EvidenceDocumentError:
                    pass
            raise
        except OSError as exc:
            if os.path.lexists(temporary):
                staged = os.lstat(temporary)
                if staged.st_nlink == 1:
                    self._remove_verified_stage_locked(
                        temporary,
                        payload_sha256=payload_sha256,
                        maximum_bytes=len(payload),
                        purpose=purpose,
                    )
            raise StoreSafetyError(f"atomic {purpose} commit failed: {exc}") from exc
        return True

    def _pending_receipt_locked(
        self, request_sha256: str, request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bytes, str] | None:
        candidates = self._intent_candidates_locked("receipt", request_sha256)
        if not candidates:
            return None
        if len(candidates) != 1:
            raise StoreSafetyError("capture request has multiple pending receipts")
        path = candidates[0]
        _, _, payload_sha256 = self._parse_intent(path)
        metadata = os.lstat(path)
        if metadata.st_nlink != 1:
            return None
        raw = _read_regular_file(
            path,
            maximum_bytes=MAX_ACCEPTANCE_RECEIPT_BYTES,
            purpose="pending acceptance receipt",
        )
        if _sha256(raw) != payload_sha256:
            raise IntegrityError("pending acceptance receipt digest mismatch")
        receipt = validate_acceptance_receipt(raw)
        if receipt["capture_request"] != request:
            raise IntegrityError("pending receipt capture-request collision")
        if canonical_json_bytes(receipt) != raw:
            raise IntegrityError("pending acceptance receipt is not canonical")
        return receipt, raw, payload_sha256

    def _sharded_ids_locked(
        self,
        *,
        root: Path,
        filename_re: re.Pattern[str],
        maximum: int,
        purpose: str,
        kind: str,
    ) -> list[str]:
        identifiers: list[str] = []
        try:
            with os.scandir(root) as iterator:
                shards = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise StoreSafetyError(f"cannot scan {purpose} store: {exc}") from exc
        for shard in shards:
            if shard.is_symlink():
                raise StoreSafetyError(
                    f"symlink {purpose} shard is forbidden: {shard.path}"
                )
            if not _HEX_SHARD_RE.fullmatch(shard.name) or not shard.is_dir(
                follow_symlinks=False
            ):
                raise IntegrityError(f"unexpected {purpose}-store entry: {shard.path}")
            _assert_private_directory(Path(shard.path), f"{purpose} shard")
            try:
                with os.scandir(shard.path) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                raise StoreSafetyError(f"cannot scan {purpose} shard: {exc}") from exc
            for entry in entries:
                if entry.is_symlink():
                    raise StoreSafetyError(
                        f"symlink {purpose} entry is forbidden: {entry.path}"
                    )
                match = filename_re.fullmatch(entry.name)
                if match is None or not entry.is_file(follow_symlinks=False):
                    raise IntegrityError(
                        f"unexpected {purpose}-store entry: {entry.path}"
                    )
                identifier = match.group(1)
                if identifier[:2] != shard.name:
                    raise IntegrityError(f"{purpose} is stored in the wrong shard")
                file_metadata = entry.stat(follow_symlinks=False)
                if file_metadata.st_nlink == 2:
                    if (
                        self._verify_recoverable_alias_locked(
                            kind, identifier, Path(entry.path)
                        )
                        is None
                    ):
                        raise StoreSafetyError(f"unrecoverable two-link {purpose}")
                    # Link-before-destination-fsync crash states are not admitted.
                    continue
                if file_metadata.st_nlink != 1:
                    raise StoreSafetyError(f"{purpose} has an unsafe link count")
                identifiers.append(identifier)
                if len(identifiers) > maximum:
                    raise EvidenceDocumentError(
                        f"{purpose} store exceeds {maximum} records"
                    )
        return identifiers

    def _receipt_id_pairs_locked(self) -> list[tuple[str, str]]:
        """Return committed ``(capture request, receipt)`` content identities."""

        identifiers: list[tuple[str, str]] = []
        try:
            with os.scandir(self.receipts_root) as iterator:
                shards = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise StoreSafetyError(
                f"cannot scan acceptance receipt store: {exc}"
            ) from exc
        for shard in shards:
            if (
                shard.is_symlink()
                or not _HEX_SHARD_RE.fullmatch(shard.name)
                or not shard.is_dir(follow_symlinks=False)
            ):
                raise IntegrityError(f"unexpected receipt shard: {shard.path}")
            _assert_private_directory(Path(shard.path), "acceptance receipt shard")
            with os.scandir(shard.path) as iterator:
                request_entries = sorted(iterator, key=lambda item: item.name)
            for request_entry in request_entries:
                request_sha256 = request_entry.name
                if (
                    request_entry.is_symlink()
                    or not _SHA256_RE.fullmatch(request_sha256)
                    or request_sha256[:2] != shard.name
                    or not request_entry.is_dir(follow_symlinks=False)
                ):
                    raise IntegrityError(
                        f"unexpected receipt request entry: {request_entry.path}"
                    )
                request_directory = Path(request_entry.path)
                _assert_private_directory(
                    request_directory, "acceptance receipt request directory"
                )
                with os.scandir(request_directory) as iterator:
                    receipt_entries = sorted(iterator, key=lambda item: item.name)
                if len(receipt_entries) > 1:
                    raise IntegrityError(
                        "capture request has colliding create-once receipts"
                    )
                for receipt_entry in receipt_entries:
                    if receipt_entry.is_symlink():
                        raise StoreSafetyError(
                            "symlink acceptance receipt is forbidden"
                        )
                    match = _RECEIPT_FILENAME_RE.fullmatch(receipt_entry.name)
                    if match is None or not receipt_entry.is_file(
                        follow_symlinks=False
                    ):
                        raise IntegrityError("unexpected acceptance receipt entry")
                    receipt_sha256 = match.group(1)
                    metadata = receipt_entry.stat(follow_symlinks=False)
                    if metadata.st_nlink == 2:
                        if (
                            self._verify_recoverable_alias_locked(
                                "receipt",
                                request_sha256,
                                Path(receipt_entry.path),
                            )
                            is None
                        ):
                            raise StoreSafetyError(
                                "unrecoverable two-link acceptance receipt"
                            )
                        continue
                    if metadata.st_nlink != 1:
                        raise StoreSafetyError(
                            "acceptance receipt has an unsafe link count"
                        )
                    identifiers.append((request_sha256, receipt_sha256))
                    if len(identifiers) > MAX_STORED_RECEIPTS:
                        raise EvidenceDocumentError(
                            f"acceptance receipt store exceeds {MAX_STORED_RECEIPTS} records"
                        )
        return identifiers

    def _manifest_ids_locked(self) -> list[str]:
        return self._sharded_ids_locked(
            root=self.manifests_root,
            filename_re=_MANIFEST_FILENAME_RE,
            maximum=MAX_STORED_MANIFESTS,
            purpose="manifest",
            kind="manifest",
        )

    def _load_receipt_locked(
        self, request_sha256: str, receipt_sha256: str
    ) -> dict[str, Any]:
        path = self.receipt_path(request_sha256, receipt_sha256)
        _assert_directory_path_without_symlinks(path.parent, allow_missing=False)
        self._assert_private_tree(path.parent)
        raw = _read_regular_file(
            path,
            maximum_bytes=MAX_ACCEPTANCE_RECEIPT_BYTES,
            purpose="acceptance receipt",
        )
        receipt = validate_acceptance_receipt(raw)
        if _sha256(raw) != receipt_sha256:
            raise IntegrityError("acceptance receipt filename/content mismatch")
        if receipt["capture_request_sha256"] != request_sha256:
            raise IntegrityError("acceptance receipt filename/request mismatch")
        if canonical_json_bytes(receipt) != raw:
            raise IntegrityError("stored acceptance receipt is not canonical JSON")
        return receipt

    def _existing_receipt_locked(
        self, request_sha256: str
    ) -> tuple[dict[str, Any], str] | None:
        request_directory = self.receipts_root / request_sha256[:2] / request_sha256
        if not os.path.lexists(request_directory):
            return None
        _assert_private_directory(
            request_directory, "acceptance receipt request directory"
        )
        with os.scandir(request_directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        if not entries:
            return None
        if len(entries) != 1:
            raise IntegrityError("capture request has colliding create-once receipts")
        entry = entries[0]
        match = _RECEIPT_FILENAME_RE.fullmatch(entry.name)
        if (
            entry.is_symlink()
            or match is None
            or not entry.is_file(follow_symlinks=False)
        ):
            raise IntegrityError("unexpected acceptance receipt entry")
        receipt_sha256 = match.group(1)
        path = Path(entry.path)
        metadata = os.lstat(path)
        if metadata.st_nlink == 2:
            recoverable = self._verify_recoverable_alias_locked(
                "receipt", request_sha256, path
            )
            if recoverable is None:
                raise StoreSafetyError("receipt has no recoverable staging alias")
            temporary, payload_sha256 = recoverable
            _finalize_staging_alias(
                temporary,
                path,
                payload_sha256=payload_sha256,
                maximum_bytes=MAX_ACCEPTANCE_RECEIPT_BYTES,
                purpose="acceptance receipt",
            )
        receipt = self._load_receipt_locked(request_sha256, receipt_sha256)
        # An exact-repeat request re-establishes durability for every immutable
        # dependency, including the create-once acceptance receipt.
        _fsync_regular_file(path, "acceptance receipt")
        _fsync_directory(path.parent)
        return receipt, receipt_sha256

    def _max_accepted_at_locked(self) -> str | None:
        maximum: str | None = None
        for request_sha256, receipt_sha256 in self._receipt_id_pairs_locked():
            receipt = self._load_receipt_locked(request_sha256, receipt_sha256)
            accepted_at = receipt["accepted_at"]
            if maximum is None or _timestamp_value(accepted_at) > _timestamp_value(
                maximum
            ):
                maximum = accepted_at
        return maximum

    def _max_pending_accepted_at_locked(self) -> str | None:
        """Return the greatest clock value already sealed in a receipt intent."""

        maximum: str | None = None
        inspected = 0
        try:
            with os.scandir(self.staging_root) as iterator:
                for entry in iterator:
                    if entry.name == ".recovery.lock":
                        continue
                    if inspected >= MAX_STAGING_INSPECTIONS:
                        raise EvidenceDocumentError(
                            "pending receipt clock scan exceeded its bounded "
                            "inspection budget; run recover_staging in batches"
                        )
                    inspected += 1
                    match = _STAGING_INTENT_RE.fullmatch(entry.name)
                    if match is None or match.group(1) != "receipt":
                        continue
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        raise StoreSafetyError(
                            "pending acceptance receipt intent is not a regular file"
                        )
                    kind, request_sha256, payload_sha256 = match.groups()
                    assert kind == "receipt"
                    path = Path(entry.path)
                    metadata = entry.stat(follow_symlinks=False)
                    if metadata.st_nlink not in {1, 2}:
                        raise StoreSafetyError(
                            "pending acceptance receipt has an unsafe link count"
                        )
                    if metadata.st_nlink == 2:
                        destination = self.receipt_path(request_sha256, payload_sha256)
                        if self._verify_recoverable_alias_locked(
                            "receipt", request_sha256, destination
                        ) != (path, payload_sha256):
                            raise StoreSafetyError(
                                "pending receipt does not match its recovery alias"
                            )
                    raw = _read_regular_file(
                        path,
                        maximum_bytes=MAX_ACCEPTANCE_RECEIPT_BYTES,
                        purpose="pending acceptance receipt clock",
                        expected_nlink=metadata.st_nlink,
                    )
                    if _sha256(raw) != payload_sha256:
                        raise IntegrityError(
                            "pending acceptance receipt intent digest mismatch"
                        )
                    receipt = validate_acceptance_receipt(raw)
                    if canonical_json_bytes(receipt) != raw:
                        raise IntegrityError(
                            "pending acceptance receipt is not canonical JSON"
                        )
                    if receipt["capture_request_sha256"] != request_sha256:
                        raise IntegrityError(
                            "pending acceptance receipt intent request mismatch"
                        )
                    accepted_at = receipt["accepted_at"]
                    if maximum is None or _timestamp_value(
                        accepted_at
                    ) > _timestamp_value(maximum):
                        maximum = accepted_at
        except OSError as exc:
            raise StoreSafetyError(
                f"cannot scan pending acceptance receipts: {exc}"
            ) from exc
        return maximum

    def _acceptance_clock_head_locked(self) -> str | None:
        committed = self._max_accepted_at_locked()
        pending = self._max_pending_accepted_at_locked()
        if committed is None:
            return pending
        if pending is None:
            return committed
        if _timestamp_value(pending) > _timestamp_value(committed):
            return pending
        return committed

    def _receipt_for_manifest_locked(self, manifest: Mapping[str, Any]) -> None:
        binding = manifest["acceptance"]
        request_sha256 = binding["capture_request_sha256"]
        receipt_sha256 = binding["receipt_sha256"]
        receipt = self._load_receipt_locked(request_sha256, receipt_sha256)
        expected_request = _capture_request_for(
            {key: manifest[key] for key in _METADATA_KEYS},
            manifest["content"]["sha256"],
            manifest["content"]["byte_size"],
        )
        if (
            receipt["capture_request"] != expected_request
            or receipt["accepted_at"] != binding["accepted_at"]
            or receipt_sha256 != binding["receipt_sha256"]
        ):
            raise IntegrityError("manifest acceptance receipt binding mismatch")

    def _reset_recovery_iterator(self) -> None:
        iterator = self._recovery_iterator
        self._recovery_iterator = None
        if iterator is not None:
            iterator.close()

    def recover_staging(
        self,
        *,
        older_than_seconds: int = DEFAULT_STAGING_MAX_AGE_SECONDS,
        maximum_files: int = MAX_STAGING_FILES,
        maximum_entries: int | None = None,
        now: float | None = None,
    ) -> int:
        """Recover or remove at most one bounded batch of staging entries."""

        if (
            type(older_than_seconds) is not int
            or older_than_seconds < 60
            or older_than_seconds > 365 * 24 * 60 * 60
        ):
            raise EvidenceDocumentError(
                "older_than_seconds must be from 60 seconds through 365 days"
            )
        if type(maximum_files) is not int or not (
            1 <= maximum_files <= MAX_STAGING_FILES
        ):
            raise EvidenceDocumentError(
                f"maximum_files must be from 1 through {MAX_STAGING_FILES}"
            )
        if maximum_entries is None:
            maximum_entries = maximum_files
        if type(maximum_entries) is not int or not (
            1 <= maximum_entries <= MAX_STAGING_INSPECTIONS
        ):
            raise EvidenceDocumentError(
                "maximum_entries must be from 1 through " f"{MAX_STAGING_INSPECTIONS}"
            )
        current_time = time.time() if now is None else now
        if type(current_time) not in (int, float) or not (
            float("-inf") < float(current_time) < float("inf")
        ):
            raise EvidenceDocumentError("now must be a finite Unix timestamp")
        cutoff = float(current_time) - older_than_seconds
        self._ensure_layout()
        with self._store_guard(exclusive=True):
            actions: list[tuple[str, Path, tuple[str, str, str] | None]] = []
            try:
                if self._recovery_iterator is None:
                    self._recovery_iterator = os.scandir(self.staging_root)
                inspected = 0
                while inspected < maximum_entries:
                    try:
                        entry = next(self._recovery_iterator)
                    except StopIteration:
                        self._reset_recovery_iterator()
                        break
                    if entry.name == ".recovery.lock":
                        continue
                    inspected += 1
                    try:
                        path = Path(entry.path)
                        if entry.is_symlink():
                            raise StoreSafetyError(f"unexpected staging entry: {path}")
                        intent = _STAGING_INTENT_RE.fullmatch(entry.name)
                        legacy = _LEGACY_STAGING_FILENAME_RE.fullmatch(entry.name)
                        if intent is None and legacy is None:
                            raise StoreSafetyError(f"unexpected staging entry: {path}")
                        metadata = entry.stat(follow_symlinks=False)
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_uid != os.geteuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600
                            or metadata.st_nlink not in {1, 2}
                        ):
                            raise StoreSafetyError(f"unsafe staging entry: {path}")
                        if legacy is not None:
                            if metadata.st_nlink != 1:
                                raise StoreSafetyError(
                                    "legacy staging debris has aliases"
                                )
                            if metadata.st_mtime <= cutoff:
                                actions.append(("legacy-remove", path, None))
                        else:
                            parsed = self._parse_intent(path)
                            kind, destination_key, payload_sha256 = parsed
                            if metadata.st_nlink == 2:
                                destination = self._destination_for_intent(
                                    kind, destination_key, payload_sha256
                                )
                                recoverable = self._verify_recoverable_alias_locked(
                                    kind, destination_key, destination
                                )
                                if recoverable != (path, payload_sha256):
                                    raise StoreSafetyError(
                                        "staging alias does not match bound destination"
                                    )
                                actions.append(("finalize", path, parsed))
                            elif metadata.st_mtime <= cutoff:
                                payload = _read_regular_file(
                                    path,
                                    maximum_bytes=self._intent_limit(kind),
                                    purpose=f"stale staged {self._intent_purpose(kind)}",
                                )
                                if _sha256(payload) != payload_sha256:
                                    raise IntegrityError(
                                        "stale staging intent digest mismatch"
                                    )
                                actions.append(("intent-remove", path, parsed))
                        if len(actions) >= maximum_files:
                            break
                    except FileNotFoundError:
                        # A prior batch or another cooperating process may have
                        # removed this entry while the process-local directory
                        # cursor was between locked calls.
                        continue
            except OSError as exc:
                self._reset_recovery_iterator()
                raise StoreSafetyError(f"cannot scan staging directory: {exc}") from exc
            except Exception:
                self._reset_recovery_iterator()
                raise

            completed = 0
            try:
                for action, path, parsed in sorted(
                    actions, key=lambda item: item[1].name
                ):
                    if action == "finalize":
                        assert parsed is not None
                        kind, destination_key, payload_sha256 = parsed
                        _finalize_staging_alias(
                            path,
                            self._destination_for_intent(
                                kind, destination_key, payload_sha256
                            ),
                            payload_sha256=payload_sha256,
                            maximum_bytes=self._intent_limit(kind),
                            purpose=self._intent_purpose(kind),
                        )
                    elif action == "intent-remove":
                        assert parsed is not None
                        kind, _, payload_sha256 = parsed
                        self._remove_verified_stage_locked(
                            path,
                            payload_sha256=payload_sha256,
                            maximum_bytes=self._intent_limit(kind),
                            purpose=self._intent_purpose(kind),
                        )
                    else:
                        metadata = os.lstat(path)
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_uid != os.geteuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600
                            or metadata.st_nlink != 1
                        ):
                            raise StoreSafetyError("legacy staging debris changed")
                        path.unlink()
                        _fsync_directory(self.staging_root)
                    completed += 1
            except Exception:
                self._reset_recovery_iterator()
                raise
            return completed

    def ingest(
        self,
        content: bytes | bytearray | memoryview,
        metadata: Mapping[str, Any] | bytes | str,
    ) -> StoredEvidenceDocument:
        """Atomically retain one capture request and trusted acceptance receipt."""

        raw = _content_bytes(content)
        if not raw:
            raise EvidenceDocumentError("content cannot be empty")
        if len(raw) > self.max_document_bytes:
            raise EvidenceDocumentError(
                f"content is {len(raw)} bytes; limit is {self.max_document_bytes}"
            )
        normalized_metadata = _metadata_document(metadata)
        content_sha256 = _sha256(raw)
        request = _capture_request_for(normalized_metadata, content_sha256, len(raw))
        request = validate_capture_request(request)
        request_sha256 = _sha256(canonical_json_bytes(request))

        self._ensure_layout()
        with self._store_guard(exclusive=True):
            existing_receipt = self._existing_receipt_locked(request_sha256)
            pending = None
            if existing_receipt is None:
                pending = self._pending_receipt_locked(request_sha256, request)
            if existing_receipt is not None:
                receipt, receipt_sha256 = existing_receipt
                receipt_bytes = canonical_json_bytes(receipt)
                receipt_created = False
            elif pending is not None:
                receipt, receipt_bytes, receipt_sha256 = pending
                # This receipt's time was already sampled, written, and fsynced
                # before the crash.  A later unrelated receipt must not strand
                # this exact request by retroactively treating it as rollback.
                receipt_created = self._install_immutable_locked(
                    self.receipt_path(request_sha256, receipt_sha256),
                    receipt_bytes,
                    "acceptance receipt",
                    kind="receipt",
                    destination_key=request_sha256,
                )
            else:
                previous_maximum = self._acceptance_clock_head_locked()
                clock_value = self._acceptance_clock(deepcopy(request))
                accepted_at = _trusted_timestamp(clock_value, "accepted_at")
                if _timestamp_value(accepted_at) < _timestamp_value(
                    normalized_metadata["collected_at"]
                ):
                    raise AcceptanceClockError(
                        "accepted_at cannot precede caller collected_at"
                    )
                if previous_maximum is not None and _timestamp_value(
                    accepted_at
                ) < _timestamp_value(previous_maximum):
                    raise AcceptanceClockError(
                        "acceptance clock moved backwards from the sealed receipt head"
                    )
                receipt = _acceptance_receipt_for(request, accepted_at)
                receipt = validate_acceptance_receipt(receipt)
                receipt_bytes = canonical_json_bytes(receipt)
                receipt_sha256 = _sha256(receipt_bytes)
                receipt_created = self._install_immutable_locked(
                    self.receipt_path(request_sha256, receipt_sha256),
                    receipt_bytes,
                    "acceptance receipt",
                    kind="receipt",
                    destination_key=request_sha256,
                )

            if receipt["capture_request"] != request:
                raise IntegrityError("acceptance receipt capture-request collision")
            acceptance = {
                "accepted_at": receipt["accepted_at"],
                "capture_request_sha256": request_sha256,
                "receipt_sha256": receipt_sha256,
            }
            manifest = _manifest_for(
                normalized_metadata, content_sha256, len(raw), acceptance
            )
            manifest = validate_manifest(manifest, content=raw)
            manifest_bytes = canonical_json_bytes(manifest)
            manifest_sha256 = _sha256(manifest_bytes)

            content_path = self.content_path(content_sha256)
            manifest_path = self.manifest_path(manifest_sha256)
            content_created = self._install_immutable_locked(
                content_path,
                raw,
                "content object",
                kind="content",
                destination_key=content_sha256,
            )
            manifest_created = self._install_immutable_locked(
                manifest_path,
                manifest_bytes,
                "manifest",
                kind="manifest",
                destination_key=manifest_sha256,
            )
            return StoredEvidenceDocument(
                manifest_sha256=manifest_sha256,
                content_sha256=content_sha256,
                byte_size=len(raw),
                manifest_path=manifest_path,
                content_path=content_path,
                receipt_path=self.receipt_path(request_sha256, receipt_sha256),
                manifest=deepcopy(manifest),
                accepted_at=receipt["accepted_at"],
                receipt_sha256=receipt_sha256,
                content_created=content_created,
                receipt_created=receipt_created,
                manifest_created=manifest_created,
            )

    def _load_manifest_locked(
        self, manifest_sha256: str, *, verify_content: bool = True
    ) -> dict[str, Any]:
        path = self.manifest_path(manifest_sha256)
        _assert_directory_path_without_symlinks(path.parent, allow_missing=False)
        self._assert_private_tree(path.parent)
        raw = _read_regular_file(
            path, maximum_bytes=MAX_MANIFEST_BYTES, purpose="manifest"
        )
        if _sha256(raw) != manifest_sha256:
            raise IntegrityError("manifest filename does not match its SHA-256")
        manifest = validate_manifest(raw)
        if canonical_json_bytes(manifest) != raw:
            raise IntegrityError("stored manifest is not canonical JSON")
        self._receipt_for_manifest_locked(manifest)
        if verify_content:
            self._content_for_manifest_locked(manifest)
        return manifest

    def load_manifest(
        self, manifest_sha256: str, *, verify_content: bool = True
    ) -> dict[str, Any]:
        """Load and validate a manifest under the shared transaction barrier."""

        self._ensure_layout()
        with self._store_guard(exclusive=False):
            return self._load_manifest_locked(
                manifest_sha256, verify_content=verify_content
            )

    def _content_for_manifest_locked(self, manifest: Mapping[str, Any]) -> bytes:
        content_record = manifest["content"]
        digest = content_record["sha256"]
        expected_size = content_record["byte_size"]
        path = self.content_path(digest)
        _assert_directory_path_without_symlinks(path.parent, allow_missing=False)
        self._assert_private_tree(path.parent)
        raw = _read_regular_file(
            path, maximum_bytes=expected_size, purpose="content object"
        )
        if len(raw) != expected_size or _sha256(raw) != digest:
            raise IntegrityError("manifest/content digest or byte-size mismatch")
        return raw

    # Compatibility for existing internal tests; callers must already hold a lock.
    _content_for_manifest = _content_for_manifest_locked

    def read_content(self, manifest_sha256: str) -> bytes:
        """Read archival bytes under one shared manifest/content transaction."""

        self._ensure_layout()
        with self._store_guard(exclusive=False):
            manifest = self._load_manifest_locked(manifest_sha256, verify_content=False)
            return self._content_for_manifest_locked(manifest)

    def _manifest_ids(self) -> list[str]:
        """Compatibility wrapper returning committed IDs under a shared lock."""

        self._ensure_layout()
        with self._store_guard(exclusive=False):
            return self._manifest_ids_locked()

    def build_training_cut(
        self,
        as_of: str | datetime,
        *,
        rights_ledger: Mapping[str, Any] | bytes | str,
        trusted_rights_ledger_sha256: str,
        policy: str = TEXT_TRAINING_POLICY_ID,
    ) -> TrainingCut:
        """Build a deterministic cut under one shared store transaction."""

        normalized_as_of = _as_of_timestamp(as_of)
        if policy != TEXT_TRAINING_POLICY_ID:
            raise TrainingPolicyError(
                f"unsupported fail-closed training policy: {policy!r}"
            )
        if rights_ledger is None:
            raise TrainingPolicyError("an explicit complete rights ledger is required")
        trusted_head = _sha256_value(
            trusted_rights_ledger_sha256, "trusted_rights_ledger_sha256"
        )
        assert trusted_head is not None
        complete_ledger = validate_rights_ledger(rights_ledger)
        actual_head = _sha256(canonical_json_bytes(complete_ledger))
        if actual_head != trusted_head:
            raise IntegrityError(
                "complete rights ledger does not match the trusted out-of-band head"
            )
        cutoff = _timestamp_value(normalized_as_of)
        visible_ledger = _rights_ledger_as_of(complete_ledger, cutoff)

        self._ensure_layout()
        with self._store_guard(exclusive=False):
            groups: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
            for manifest_sha256 in self._manifest_ids_locked():
                manifest = self._load_manifest_locked(
                    manifest_sha256, verify_content=False
                )
                if any(
                    _timestamp_value(value) > cutoff
                    for value in (
                        manifest["knowledge_time"],
                        manifest["collected_at"],
                        manifest["acceptance"]["accepted_at"],
                    )
                ):
                    continue
                key = (manifest["source"]["id"], manifest["content"]["sha256"])
                group = groups.setdefault(key, [])
                group.append((manifest_sha256, manifest))
                if len(group) > MAX_PROVENANCE_PER_RECORD:
                    raise EvidenceDocumentError(
                        "training candidate exceeds the provenance-per-record bound"
                    )
                if len(groups) > MAX_TRAINING_RECORDS:
                    raise EvidenceDocumentError(
                        f"training candidates exceed {MAX_TRAINING_RECORDS} dedupe groups"
                    )

            records: list[dict[str, Any]] = []
            total_content_bytes = 0
            for (source_id, content_sha256), manifests in sorted(groups.items()):
                manifests.sort(key=lambda item: item[0])
                effective_rights = _resolve_effective_rights(
                    source_id=source_id,
                    content_sha256=content_sha256,
                    manifests=manifests,
                    visible_ledger=visible_ledger,
                    cutoff=cutoff,
                )
                if (
                    effective_rights is None
                    or effective_rights["training_use"] != "full_text"
                ):
                    continue
                if not all(
                    _is_textual_media_type(manifest["media_type"])
                    for _, manifest in manifests
                ):
                    continue
                sizes = {manifest["content"]["byte_size"] for _, manifest in manifests}
                if len(sizes) != 1:
                    raise IntegrityError(
                        f"deduplicated manifests disagree on byte size for {content_sha256}"
                    )
                raw = self._content_for_manifest_locked(manifests[0][1])
                total_content_bytes += len(raw)
                if total_content_bytes > MAX_TRAINING_CONTENT_BYTES:
                    raise EvidenceDocumentError(
                        "training content exceeds the v1 cumulative byte limit"
                    )
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise TrainingPolicyError(
                        f"full_text subject {source_id}/{content_sha256} is not valid UTF-8"
                    ) from exc
                provenance = [
                    {
                        "manifest_sha256": manifest_sha256,
                        "canonical_url": manifest["source"]["canonical_url"],
                        "media_type": manifest["media_type"],
                        "language": manifest["language"],
                        "event_time": manifest["event_time"],
                        "publication_time": manifest["publication_time"],
                        "knowledge_time": manifest["knowledge_time"],
                        "collected_at": manifest["collected_at"],
                        "acceptance": deepcopy(manifest["acceptance"]),
                        "collection": deepcopy(manifest["collection"]),
                        "retention_class": manifest["retention_class"],
                        "declared_rights": deepcopy(manifest["rights"]),
                    }
                    for manifest_sha256, manifest in manifests
                ]
                manifest_sha256s = [item["manifest_sha256"] for item in provenance]
                records.append(
                    {
                        "source": {
                            "id": source_id,
                            "canonical_urls": sorted(
                                {item["canonical_url"] for item in provenance}
                            ),
                        },
                        "manifest_sha256s": manifest_sha256s,
                        "media_types": sorted(
                            {item["media_type"] for item in provenance}
                        ),
                        "languages": sorted({item["language"] for item in provenance}),
                        "provenance": provenance,
                        "effective_rights": effective_rights,
                        "content": {
                            "sha256": content_sha256,
                            "byte_size": next(iter(sizes)),
                            "encoding": "utf-8",
                            "text": text,
                            "base64": base64.b64encode(raw).decode("ascii"),
                        },
                    }
                )

            cut_document = {
                "spec_version": CUT_SPEC_VERSION,
                "canonicalization": CANONICALIZATION,
                "as_of": normalized_as_of,
                "policy": deepcopy(_TEXT_TRAINING_POLICY),
                "rights_ledger": visible_ledger,
                "records": records,
            }
            canonical = canonical_json_bytes(cut_document)
            if len(canonical) > MAX_TRAINING_CUT_BYTES:
                raise EvidenceDocumentError(
                    f"canonical training cut exceeds {MAX_TRAINING_CUT_BYTES} bytes"
                )
            return TrainingCut._from_validated_builder(
                cut_sha256=_sha256(canonical),
                as_of=normalized_as_of,
                canonical_bytes=canonical,
                complete_rights_ledger=complete_ledger,
                trusted_rights_ledger_sha256=trusted_head,
            )


def _validate_training_cut_structure(
    *, cut_sha256: str, as_of: str, canonical_bytes: bytes
) -> dict[str, Any]:
    """Validate identities and embedded invariants, but not ledger completeness."""

    digest = _sha256_value(cut_sha256, "cut_sha256")
    assert digest is not None
    if type(canonical_bytes) is not bytes:
        raise IntegrityError("training cut canonical_bytes must be exact bytes")
    if len(canonical_bytes) > MAX_TRAINING_CUT_BYTES:
        raise IntegrityError("training cut exceeds the canonical byte limit")
    if _sha256(canonical_bytes) != digest:
        raise IntegrityError("training cut bytes do not match cut_sha256")
    normalized_as_of = _as_of_timestamp(as_of)
    if type(as_of) is not str or normalized_as_of != as_of:
        raise IntegrityError("TrainingCut.as_of must be its canonical timestamp string")
    parsed = strict_json_loads(
        canonical_bytes,
        maximum_bytes=MAX_TRAINING_CUT_BYTES,
        purpose="training cut",
    )
    document = _exact_object(
        parsed,
        frozenset(
            {
                "spec_version",
                "canonicalization",
                "as_of",
                "policy",
                "rights_ledger",
                "records",
            }
        ),
        "training cut",
    )
    if canonical_json_bytes(document) != canonical_bytes:
        raise IntegrityError("training cut bytes are not canonical JSON")
    if document["spec_version"] != CUT_SPEC_VERSION:
        raise IntegrityError("unsupported training cut spec_version")
    if document["canonicalization"] != CANONICALIZATION:
        raise IntegrityError("unsupported training cut canonicalization")
    if document["as_of"] != as_of:
        raise IntegrityError(
            "training cut document as_of does not match TrainingCut.as_of"
        )
    if document["policy"] != _TEXT_TRAINING_POLICY:
        raise TrainingPolicyError("training cut policy does not equal the v1 policy")

    visible_ledger = validate_rights_ledger(document["rights_ledger"])
    if visible_ledger != document["rights_ledger"]:
        raise IntegrityError("training cut rights ledger is not canonically ordered")
    cutoff = _timestamp_value(as_of)
    if any(
        _timestamp_value(entry["decision"]["knowledge_time"]) > cutoff
        for entry in visible_ledger["decisions"]
    ):
        raise IntegrityError("training cut embeds a future-knowledge rights decision")

    records_value = document["records"]
    if type(records_value) is not list:
        raise IntegrityError("training cut records must be an array")
    if len(records_value) > MAX_TRAINING_RECORDS:
        raise IntegrityError("training cut exceeds the record bound")
    record_keys = frozenset(
        {
            "source",
            "manifest_sha256s",
            "media_types",
            "languages",
            "provenance",
            "effective_rights",
            "content",
        }
    )
    provenance_keys = frozenset(
        {
            "manifest_sha256",
            "canonical_url",
            "media_type",
            "language",
            "event_time",
            "publication_time",
            "knowledge_time",
            "collected_at",
            "acceptance",
            "collection",
            "retention_class",
            "declared_rights",
        }
    )

    prior_key: tuple[str, str] | None = None
    total_content_bytes = 0
    for record_index, raw_record in enumerate(records_value):
        path = f"training cut.records[{record_index}]"
        record = _exact_object(raw_record, record_keys, path)
        source = _exact_object(
            record["source"], frozenset({"id", "canonical_urls"}), f"{path}.source"
        )
        source_id = _safe_identifier(source["id"], f"{path}.source.id")

        content_value = _exact_object(
            record["content"],
            frozenset({"sha256", "byte_size", "encoding", "text", "base64"}),
            f"{path}.content",
        )
        content = _content_record(
            {
                "sha256": content_value["sha256"],
                "byte_size": content_value["byte_size"],
            },
            f"{path}.content",
        )
        if (
            content_value["encoding"] != "utf-8"
            or type(content_value["text"]) is not str
        ):
            raise IntegrityError(f"{path}.content must contain UTF-8 text")
        try:
            text_bytes = content_value["text"].encode("utf-8")
        except UnicodeEncodeError as exc:
            raise IntegrityError(
                f"{path}.content.text is not Unicode scalar text"
            ) from exc
        encoded_base64 = content_value["base64"]
        if type(encoded_base64) is not str:
            raise IntegrityError(f"{path}.content.base64 must be text")
        try:
            decoded = base64.b64decode(encoded_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise IntegrityError(f"{path}.content.base64 is invalid") from exc
        if (
            decoded != text_bytes
            or base64.b64encode(decoded).decode("ascii") != encoded_base64
            or len(decoded) != content["byte_size"]
            or _sha256(decoded) != content["sha256"]
        ):
            raise IntegrityError(f"{path}.content identities disagree")
        total_content_bytes += len(decoded)
        if total_content_bytes > MAX_TRAINING_CONTENT_BYTES:
            raise IntegrityError("training cut exceeds the cumulative content bound")

        provenance_value = record["provenance"]
        if type(provenance_value) is not list or not (
            1 <= len(provenance_value) <= MAX_PROVENANCE_PER_RECORD
        ):
            raise IntegrityError(f"{path}.provenance has an invalid size")
        manifests: list[tuple[str, dict[str, Any]]] = []
        for provenance_index, raw_provenance in enumerate(provenance_value):
            provenance_path = f"{path}.provenance[{provenance_index}]"
            provenance = _exact_object(raw_provenance, provenance_keys, provenance_path)
            manifest_sha256 = _sha256_value(
                provenance["manifest_sha256"],
                f"{provenance_path}.manifest_sha256",
            )
            assert manifest_sha256 is not None
            metadata = _validate_metadata(
                {
                    "source": {
                        "id": source_id,
                        "canonical_url": provenance["canonical_url"],
                    },
                    "media_type": provenance["media_type"],
                    "language": provenance["language"],
                    "event_time": provenance["event_time"],
                    "publication_time": provenance["publication_time"],
                    "knowledge_time": provenance["knowledge_time"],
                    "collected_at": provenance["collected_at"],
                    "collection": provenance["collection"],
                    "retention_class": provenance["retention_class"],
                    "rights": provenance["declared_rights"],
                }
            )
            request = _capture_request_for(
                metadata, content["sha256"], content["byte_size"]
            )
            request_sha256 = _sha256(canonical_json_bytes(request))
            acceptance = _acceptance_binding(
                provenance["acceptance"],
                capture_request_sha256=request_sha256,
            )
            manifest = validate_manifest(
                _manifest_for(
                    metadata,
                    content["sha256"],
                    content["byte_size"],
                    acceptance,
                )
            )
            if _sha256(canonical_json_bytes(manifest)) != manifest_sha256:
                raise IntegrityError(f"{provenance_path} does not bind its manifest")
            if any(
                _timestamp_value(value) > cutoff
                for value in (
                    manifest["knowledge_time"],
                    manifest["collected_at"],
                    manifest["acceptance"]["accepted_at"],
                )
            ):
                raise IntegrityError(f"{provenance_path} leaks a future manifest")
            manifests.append((manifest_sha256, manifest))

        manifest_ids = [manifest_sha256 for manifest_sha256, _ in manifests]
        if manifest_ids != sorted(manifest_ids) or len(set(manifest_ids)) != len(
            manifest_ids
        ):
            raise IntegrityError(f"{path}.provenance is not uniquely manifest-sorted")
        if record["manifest_sha256s"] != manifest_ids:
            raise IntegrityError(f"{path}.manifest_sha256s disagrees with provenance")
        expected_urls = sorted(
            {manifest["source"]["canonical_url"] for _, manifest in manifests}
        )
        expected_media = sorted({manifest["media_type"] for _, manifest in manifests})
        expected_languages = sorted({manifest["language"] for _, manifest in manifests})
        if source["canonical_urls"] != expected_urls:
            raise IntegrityError(f"{path}.source.canonical_urls disagrees")
        if record["media_types"] != expected_media or not all(
            _is_textual_media_type(value) for value in expected_media
        ):
            raise IntegrityError(f"{path}.media_types disagrees or is ineligible")
        if record["languages"] != expected_languages:
            raise IntegrityError(f"{path}.languages disagrees")
        expected_rights = _resolve_effective_rights(
            source_id=source_id,
            content_sha256=content["sha256"],
            manifests=manifests,
            visible_ledger=visible_ledger,
            cutoff=cutoff,
        )
        if (
            expected_rights is None
            or expected_rights["training_use"] != "full_text"
            or record["effective_rights"] != expected_rights
        ):
            raise IntegrityError(f"{path}.effective_rights is not authorized")
        key = (source_id, content["sha256"])
        if prior_key is not None and key <= prior_key:
            raise IntegrityError("training cut records are not uniquely key-sorted")
        prior_key = key
    return deepcopy(document)


def validate_training_cut(
    *,
    cut_sha256: str,
    as_of: str,
    canonical_bytes: bytes,
    complete_rights_ledger: Mapping[str, Any] | bytes | str,
    trusted_rights_ledger_sha256: str,
) -> dict[str, Any]:
    """Validate a cut against an out-of-band trusted complete rights ledger.

    Embedded bytes alone cannot prove that a visible revocation was not omitted.
    The complete ledger and its separately trusted head are therefore mandatory
    for every public validation path.
    """

    if complete_rights_ledger is None:
        raise TrainingPolicyError(
            "an explicit complete rights ledger is required to validate a cut"
        )
    trusted_head = _sha256_value(
        trusted_rights_ledger_sha256, "trusted_rights_ledger_sha256"
    )
    assert trusted_head is not None
    complete_ledger = validate_rights_ledger(complete_rights_ledger)
    if _sha256(canonical_json_bytes(complete_ledger)) != trusted_head:
        raise IntegrityError(
            "complete rights ledger does not match the trusted out-of-band head"
        )
    document = _validate_training_cut_structure(
        cut_sha256=cut_sha256,
        as_of=as_of,
        canonical_bytes=canonical_bytes,
    )
    expected_visible = _rights_ledger_as_of(
        complete_ledger, _timestamp_value(document["as_of"])
    )
    if document["rights_ledger"] != expected_visible:
        raise IntegrityError(
            "training cut rights projection does not match the trusted complete ledger"
        )
    return document


def _is_textual_media_type(media_type: str) -> bool:
    if media_type.startswith(_TEXT_MEDIA_TYPE_PREFIXES):
        return True
    subtype = media_type.split("/", 1)[1]
    return media_type in _TEXT_MEDIA_TYPES or subtype.endswith(
        _TEXT_MEDIA_TYPE_SUFFIXES
    )


def ingest_evidence_document(
    content: bytes | bytearray | memoryview,
    metadata: Mapping[str, Any] | bytes | str,
    *,
    store_root: str | os.PathLike[str],
    max_document_bytes: int = MAX_DOCUMENT_BYTES,
    acceptance_clock: Callable[[Mapping[str, Any]], str | datetime] | None = None,
) -> StoredEvidenceDocument:
    """Convenience wrapper requiring an explicit store root."""

    return EvidenceDocumentStore(
        store_root,
        max_document_bytes=max_document_bytes,
        acceptance_clock=acceptance_clock,
    ).ingest(content, metadata)


def build_training_cut(
    *,
    store_root: str | os.PathLike[str],
    as_of: str | datetime,
    rights_ledger: Mapping[str, Any] | bytes | str,
    trusted_rights_ledger_sha256: str,
    policy: str = TEXT_TRAINING_POLICY_ID,
) -> TrainingCut:
    """Convenience wrapper for deterministic point-in-time text cuts."""

    return EvidenceDocumentStore(store_root).build_training_cut(
        as_of,
        rights_ledger=rights_ledger,
        trusted_rights_ledger_sha256=trusted_rights_ledger_sha256,
        policy=policy,
    )


__all__ = [
    "ACCEPTANCE_RECEIPT_SPEC_VERSION",
    "AcceptanceClockError",
    "CANONICALIZATION",
    "CAPTURE_REQUEST_SPEC_VERSION",
    "CUT_SPEC_VERSION",
    "DurabilityError",
    "EvidenceDocumentError",
    "EvidenceDocumentStore",
    "HardLinkUnsupportedError",
    "IntegrityError",
    "MAX_DOCUMENT_BYTES",
    "MAX_ACCEPTANCE_RECEIPT_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_METADATA_BYTES",
    "RIGHTS_DECISION_SPEC_VERSION",
    "RIGHTS_LEDGER_SPEC_VERSION",
    "RightsConflictError",
    "SPEC_VERSION",
    "StoreSafetyError",
    "StoredEvidenceDocument",
    "TEXT_TRAINING_POLICY_ID",
    "TRAINING_USES",
    "TrainingCut",
    "TrainingPolicyError",
    "build_training_cut",
    "canonical_json_bytes",
    "capture_request_sha256",
    "empty_rights_ledger",
    "ingest_evidence_document",
    "make_rights_decision_entry",
    "rights_decision_sha256",
    "rights_ledger_sha256",
    "strict_json_loads",
    "validate_acceptance_receipt",
    "validate_capture_request",
    "validate_manifest",
    "validate_rights_decision",
    "validate_rights_ledger",
    "validate_training_cut",
]
