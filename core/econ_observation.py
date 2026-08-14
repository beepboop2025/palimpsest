"""Bitemporal contract for Palimpsest's public China-economic observations.

The existing China feeds publish convenient wide JSON rows.  That is useful for
the public board, but it is not a safe model-training contract: a row does not
say when a revision first became knowable, which source family it duplicates,
or which geographic/sector slice it describes.  This module is the narrow,
stdlib-only interchange format between Palimpsest (capture + provenance) and a
point-in-time feature/model store.

The contract is intentionally aggregate-only.  It has no respondent, person,
device or company identifier.  A collector which cannot express its output at
an aggregate series/slice level does not belong in Palimpsest; see SAFETY.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from numbers import Integral, Real
from typing import Any, Mapping
from urllib.parse import urlsplit


FREQUENCIES = frozenset({"event", "D", "W", "M", "Q", "A"})
STATUSES = frozenset({"observed", "estimate", "forecast"})

# Metadata is deliberately much narrower than a generic JSON bag.  The first
# three keys are used by the live CFETS collector.  The remainder cover parser,
# transformation and aggregate-method provenance without opening a place for
# row-level identifiers or human-authored text.
METADATA_KEYS = frozenset({
    "family",
    "method_version",
    "release_time_semantics",
    "aggregation_method",
    "aggregation_level",
    "aggregation_window",
    "seasonal_adjustment",
    "price_basis",
    "index_base",
    "source_series_id",
    "source_table_id",
    "source_release_id",
    "source_document_sha256",
    "source_manifest_sha256",
    "source_document_version",
    "parser_version",
    "schema_version",
    "transform",
    "transform_version",
    "observation_count",
    "sample_size",
    "suppression_threshold",
    "coverage_start",
    "coverage_end",
    "provenance",
    "methodology",
    "coverage",
    "frequency",
    "geography",
    "sector",
    "firm_size",
    "ownership",
})

_FORBIDDEN_METADATA_MARKERS = (
    "respondent",
    "personid",
    "personidentifier",
    "personname",
    "personal",
    "individualid",
    "individualidentifier",
    "individualname",
    "deviceid",
    "deviceidentifier",
    "companyid",
    "companyidentifier",
    "companyname",
    "firmid",
    "firmidentifier",
    "firmname",
    "userid",
    "customerid",
    "employeeid",
    "email",
    "phone",
    "freetext",
    "rawtext",
    "comment",
    "message",
    "description",
    "note",
)


def _required_string(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number (not bool)")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite")
    return normalized


def _metadata_key(key: object, path: str) -> str:
    if type(key) is not str:
        raise TypeError(f"{path} keys must be strings")
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    if any(marker in compact for marker in _FORBIDDEN_METADATA_MARKERS):
        raise ValueError(f"{path} key {key!r} is not aggregate-only")
    if key not in METADATA_KEYS:
        raise ValueError(f"{path} key {key!r} is not allowlisted")
    return key


def _json_safe(value: object, path: str = "metadata") -> Any:
    """Return builtin-only JSON data while validating nested metadata keys."""
    if value is None or type(value) in (str, bool):
        return value
    if isinstance(value, Integral) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, Real) and not isinstance(value, bool):
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{path} numbers must be finite")
        return normalized
    if isinstance(value, Mapping):
        normalized = {}
        for key, child in value.items():
            safe_key = _metadata_key(key, path)
            normalized[safe_key] = _json_safe(child, f"{path}.{safe_key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_safe(child, f"{path}[]") for child in value]
    raise TypeError(
        f"{path} must contain only JSON-safe null, bool, string, finite number, "
        "list, or allowlisted-key object values"
    )


def _aware(value: datetime, name: str) -> None:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EconomicObservation:
    """One revision of one aggregate economic measurement.

    ``period_start``/``period_end`` describe the economy; ``released_at`` is
    when this revision became public; ``collected_at`` is when Palimpsest first
    saw it.  Both clocks are required so an ``as_of`` query cannot learn from a
    release or revision that had not happened at the decision time.
    """

    series_id: str
    value: float
    unit: str
    frequency: str
    period_start: date
    period_end: date
    released_at: datetime
    collected_at: datetime
    source_id: str
    evidence_url: str
    revision: int = 0
    status: str = "observed"
    geography: str = "CN"
    sector: str = "all"
    firm_size: str = "all"
    ownership: str = "all"
    quality: float = 1.0
    raw_sha256: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("series_id", "unit", "source_id", "evidence_url"):
            _required_string(getattr(self, name), name)
        for name in ("frequency", "status", "geography", "sector", "firm_size", "ownership"):
            _required_string(getattr(self, name), name)
        if self.frequency not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {sorted(FREQUENCIES)}")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}")
        for name in ("period_start", "period_end"):
            if type(getattr(self, name)) is not date:
                raise TypeError(f"{name} must be a date (not datetime)")
        if self.period_end < self.period_start:
            raise ValueError("period_end cannot precede period_start")
        _aware(self.released_at, "released_at")
        _aware(self.collected_at, "collected_at")
        if self.collected_at < self.released_at:
            raise ValueError("collected_at cannot precede released_at")
        if isinstance(self.revision, bool) or not isinstance(self.revision, Integral):
            raise TypeError("revision must be an integer (not bool)")
        revision = int(self.revision)
        if revision < 0:
            raise ValueError("revision must be non-negative")
        value = _real(self.value, "value")
        quality = _real(self.quality, "quality")
        if not 0.0 <= quality <= 1.0:
            raise ValueError("quality must lie in [0, 1]")

        try:
            parsed_url = urlsplit(self.evidence_url)
            hostname = parsed_url.hostname
        except ValueError as exc:
            raise ValueError("evidence_url must be an absolute http(s) URL") from exc
        if parsed_url.scheme not in {"http", "https"} or not hostname:
            raise ValueError("evidence_url must be an absolute http(s) URL")
        if parsed_url.username is not None or parsed_url.password is not None:
            raise ValueError("evidence_url must not contain URL credentials")

        if self.raw_sha256 is not None:
            if type(self.raw_sha256) is not str:
                raise TypeError("raw_sha256 must be a string")
            digest = self.raw_sha256.lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("raw_sha256 must be a 64-character hex digest")
            object.__setattr__(self, "raw_sha256", digest)
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        metadata = _json_safe(self.metadata)

        object.__setattr__(self, "value", value)
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "metadata", metadata)

    @property
    def slice_key(self) -> tuple[str, ...]:
        """Natural series key excluding time, revision and source."""
        return (
            self.series_id,
            self.geography,
            self.sector,
            self.firm_size,
            self.ownership,
        )

    @property
    def vintage_key(self) -> tuple[object, ...]:
        """One source's revisions of one economic period."""
        return (*self.slice_key, self.period_start, self.period_end, self.source_id)

    @property
    def observation_id(self) -> str:
        """SHA-256 identifier for every semantic and provenance field.

        This is deliberately a full-record digest rather than a natural-key
        hash.  A changed evidence URL, raw response hash, unit, quality score,
        dimension or method annotation must not retain the ledger identity of
        the original observation.
        """
        encoded = json.dumps(
            self._record_payload(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _record_payload(self) -> dict[str, Any]:
        """Return the canonical, validated record body used for hashing."""
        return {
            "series_id": self.series_id,
            "value": self.value,
            "unit": self.unit,
            "frequency": self.frequency,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "released_at": self.released_at.isoformat(),
            "collected_at": self.collected_at.isoformat(),
            "source_id": self.source_id,
            "evidence_url": self.evidence_url,
            "revision": self.revision,
            "status": self.status,
            "geography": self.geography,
            "sector": self.sector,
            "firm_size": self.firm_size,
            "ownership": self.ownership,
            "quality": self.quality,
            "raw_sha256": self.raw_sha256,
            # Revalidation here closes the mutable-container escape hatch:
            # even if a caller mutates the original dict after construction,
            # forbidden/non-JSON metadata cannot be hashed or serialized.
            "metadata": _json_safe(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        out = self._record_payload()
        out["observation_id"] = self.observation_id
        return out

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "EconomicObservation":
        data = dict(row)
        data.pop("observation_id", None)
        data["period_start"] = date.fromisoformat(str(data["period_start"]))
        data["period_end"] = date.fromisoformat(str(data["period_end"]))
        for name in ("released_at", "collected_at"):
            data[name] = datetime.fromisoformat(str(data[name]).replace("Z", "+00:00"))
        return cls(**data)


def sha256_bytes(payload: bytes) -> str:
    """Digest helper collectors use before discarding or sealing raw bytes."""
    return hashlib.sha256(payload).hexdigest()
