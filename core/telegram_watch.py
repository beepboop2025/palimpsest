"""Reviewed, aggregate-only Telegram context for the public China desk.

ScamShield's monitoring summary is a private analyst handoff and is never
publication-eligible by itself.  This module validates the smaller public
artifact created after a human explicitly promotes selected aggregates.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Mapping


SCHEMA_VERSION = "palimpsest-telegram-watch.v1"
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_FIELDS = {
    "schema_version",
    "generated_at",
    "status",
    "relation",
    "review",
    "window",
    "sampling_frame",
    "coverage",
    "detections",
    "interpretation",
    "limitations",
}


class TelegramWatchError(ValueError):
    """A reviewed Telegram aggregate violates the public boundary."""


def canonical_json_bytes(value: Any) -> bytes:
    def reject(node: Any, path: str = "telegram_watch") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise TelegramWatchError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise TelegramWatchError(f"{path} contains a non-string key")
                reject(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                reject(child, f"{path}[{index}]")

    reject(value)
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _exact(value: Any, fields: set[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise TelegramWatchError(f"{path} does not use its exact field set")
    return value


def _text(value: Any, path: str, *, maximum: int = 2_000) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise TelegramWatchError(f"{path} must be non-empty bounded text")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise TelegramWatchError(f"{path} contains unsafe Unicode")
    return value


def _count(value: Any, path: str) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise TelegramWatchError(f"{path} must be a bounded non-negative integer")
    return value


def validate_telegram_watch(document: Mapping[str, Any]) -> None:
    """Fail closed on identifiers, raw content, weak review, or ambiguous counts."""

    top = _exact(document, _TOP_FIELDS, "telegram_watch")
    if top["schema_version"] != SCHEMA_VERSION:
        raise TelegramWatchError("invalid Telegram watch schema version")
    if not _TIMESTAMP_RE.fullmatch(str(top["generated_at"])):
        raise TelegramWatchError("generated_at must be canonical UTC")
    if top["status"] not in {"REVIEWED_CONTEXT", "REVIEWED_COVERAGE_ONLY"}:
        raise TelegramWatchError("invalid Telegram watch status")
    if top["relation"] != "aggregate-context-only-not-corroboration":
        raise TelegramWatchError("Telegram relation must remain context-only")

    review = _exact(
        top["review"],
        {"status", "reviewed_at", "reviewer_role", "source_summary_sha256", "note"},
        "review",
    )
    if review["status"] != "HUMAN_REVIEWED":
        raise TelegramWatchError("Telegram context lacks human review")
    if not _TIMESTAMP_RE.fullmatch(str(review["reviewed_at"])):
        raise TelegramWatchError("review.reviewed_at must be canonical UTC")
    if not _ROLE_RE.fullmatch(str(review["reviewer_role"])):
        raise TelegramWatchError("review.reviewer_role is invalid")
    if not _SHA256_RE.fullmatch(str(review["source_summary_sha256"])):
        raise TelegramWatchError("review source digest is invalid")
    _text(review["note"], "review.note", maximum=500)

    window = _exact(top["window"], {"start", "end", "complete"}, "window")
    for field in ("start", "end"):
        if not _TIMESTAMP_RE.fullmatch(str(window[field])):
            raise TelegramWatchError(f"window.{field} must be canonical UTC")
    if type(window["complete"]) is not bool:
        raise TelegramWatchError("window.complete must be boolean")

    sampling = _exact(
        top["sampling_frame"],
        {
            "surface",
            "universal_telegram_coverage",
            "raw_messages_included",
            "exact_iocs_included",
            "source_identifiers_included",
        },
        "sampling_frame",
    )
    if sampling != {
        "surface": "configured_public_or_operator_authorized_telegram",
        "universal_telegram_coverage": False,
        "raw_messages_included": False,
        "exact_iocs_included": False,
        "source_identifiers_included": False,
    }:
        raise TelegramWatchError("sampling frame broadens the approved privacy boundary")

    coverage = _exact(
        top["coverage"],
        {"messages_observed", "messages_flagged", "sources_observed", "collection_errors"},
        "coverage",
    )
    for key, value in coverage.items():
        _count(value, f"coverage.{key}")
    if coverage["messages_flagged"] > coverage["messages_observed"]:
        raise TelegramWatchError("flagged messages exceed observed messages")

    detections = _exact(
        top["detections"],
        {"source_status", "tier_counts", "reviewed_china_family_counts"},
        "detections",
    )
    if detections["source_status"] != "AVAILABLE_FOR_REVIEW":
        raise TelegramWatchError("source summary did not pass its coverage gate")
    for bucket in ("tier_counts", "reviewed_china_family_counts"):
        counts = detections[bucket]
        if type(counts) is not dict or len(counts) > 64:
            raise TelegramWatchError(f"detections.{bucket} must be a bounded object")
        for label, value in counts.items():
            if type(label) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", label):
                raise TelegramWatchError(f"detections.{bucket} contains an invalid label")
            _count(value, f"detections.{bucket}.{label}")
    has_families = bool(detections["reviewed_china_family_counts"])
    if (top["status"] == "REVIEWED_CONTEXT") != has_families:
        raise TelegramWatchError("status does not match reviewed family selection")

    _text(top["interpretation"], "interpretation")
    limitations = top["limitations"]
    if type(limitations) is not list or not 3 <= len(limitations) <= 12:
        raise TelegramWatchError("limitations must contain 3 to 12 statements")
    for index, value in enumerate(limitations):
        _text(value, f"limitations[{index}]")

    forbidden = {
        "raw_text", "message_text", "username", "channel", "chat_id",
        "source_pseudonym", "assessment_id", "ioc", "iocs", "url", "handle",
    }

    def scan(node: Any, path: str = "telegram_watch") -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).lower() in forbidden:
                    raise TelegramWatchError(f"{path} contains forbidden field {key!r}")
                scan(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                scan(value, f"{path}[{index}]")

    scan(document)
    canonical_json_bytes(document)


def source_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "SCHEMA_VERSION",
    "TelegramWatchError",
    "canonical_json_bytes",
    "source_digest",
    "validate_telegram_watch",
]
