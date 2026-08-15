"""Reviewed, sanitized individual signals for the Dragon Whispers desk.

Raw Telegram content is published only inside Telegram by a separate service.
This public website contract accepts analyst-authored context derived from a
verified ScamShield capsule and rejects Telegram coordinates, source identity,
raw excerpts, exact IOCs, URLs, contact details, and unreviewed allegations.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "palimpsest-dragon-whispers.v1"
RELATION = "unverified-context-only-not-evidence"
TIERS = {"CLEAN", "WATCH", "LIKELY_SCAM", "CONFIRMED_PATTERN"}
IOC_KINDS = {"handles", "phones", "channels", "wallets", "emails", "urls"}
SCRIPT_HINTS = {"latin", "devanagari", "han", "arabic", "cyrillic", "undetermined"}
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_WHISPER_ID = re.compile(r"^whisper-[0-9a-f]{24}$")
_ROLE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LABEL = re.compile(r"^[A-Z][A-Z0-9_-]{0,63}$")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}")
_URL = re.compile(r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/)")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){9,}(?!\d)")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f:]{3,}(?![0-9A-Fa-f:])")
_CRYPTO = re.compile(
    r"(?i)(?:\b0x[0-9a-f]{40}\b|\bT[1-9A-HJ-NP-Za-km-z]{33}\b|"
    r"\b(?:bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}\b)"
)

_TOP_FIELDS = {
    "schema_version", "generated_at", "status", "relation", "input_provenance",
    "method", "scope", "publication_policy", "n_entries", "entries",
}
_ENTRY_FIELDS = {
    "whisper_id", "observed_at", "published_at", "review", "signal",
    "analysis", "limitations",
}
_FORBIDDEN_FIELDS = {
    "raw_text", "message_text", "message", "caption", "media", "username",
    "channel", "chat_id", "message_id", "source", "source_url", "original_url",
    "source_pseudonym", "assessment_id", "ioc", "iocs", "url", "handle",
    "phone", "email", "wallet", "allegation", "accused", "named_party",
}


class DragonWhispersError(ValueError):
    """A public whisper crosses the reviewed/sanitized boundary."""


def canonical_json_bytes(value: Any) -> bytes:
    def reject(node: Any, path: str = "dragon_whispers") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise DragonWhispersError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise DragonWhispersError(f"{path} contains a non-string key")
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
        raise DragonWhispersError(f"{path} does not use its exact field set")
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TIMESTAMP.fullmatch(value):
        raise DragonWhispersError(f"{path} must be canonical UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise DragonWhispersError(f"{path} is not a real UTC timestamp") from exc
    return value


def _safe_text(value: Any, path: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise DragonWhispersError(f"{path} must be non-empty bounded text")
    if value != value.strip():
        raise DragonWhispersError(f"{path} has leading or trailing whitespace")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise DragonWhispersError(f"{path} contains unsafe Unicode")
    if _URL.search(value) or _EMAIL.search(value) or _HANDLE.search(value):
        raise DragonWhispersError(f"{path} contains a source, URL, or handle")
    if _PHONE.search(value) or _CRYPTO.search(value):
        raise DragonWhispersError(f"{path} contains contact or exact-IOC-like data")
    for match in _IPV4.finditer(value):
        try:
            ipaddress.ip_address(match.group(0))
        except ValueError:
            continue
        raise DragonWhispersError(f"{path} contains an IP address")
    for match in _IPV6.finditer(value):
        token = match.group(0)
        if ":" not in token:
            continue
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue
        raise DragonWhispersError(f"{path} contains an IP address")
    return value


def _count(value: Any, path: str) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise DragonWhispersError(f"{path} must be a bounded non-negative integer")
    return value


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def empty_document(generated_at: str) -> dict[str, Any]:
    """Return a valid explicit empty state, not an implied live Telegram feed."""

    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "AWAITING_REVIEW",
        "relation": RELATION,
        "input_provenance": (
            "Verified ScamShield Evidence Capsules from configured public Telegram "
            "channels, each bound to a human review receipt."
        ),
        "method": (
            "One eligible capsule may produce one reviewer-authored signal after "
            "explicit China-relevance approval and deterministic privacy validation."
        ),
        "scope": (
            "Human-reviewed, privacy-minimized analytical signals derived from "
            "configured public Telegram sources. Raw forwards remain on Telegram."
        ),
        "publication_policy": {
            "human_review_required": True,
            "raw_messages_included": False,
            "source_identifiers_included": False,
            "exact_iocs_included": False,
            "named_allegations_included": False,
            "counts_as_corroboration": False,
        },
        "n_entries": 0,
        "entries": [],
    }
    validate_dragon_whispers(document)
    return document


def validate_dragon_whispers(document: Mapping[str, Any]) -> None:
    """Validate the public individual-signal artifact and fail closed."""

    top = _exact(document, _TOP_FIELDS, "dragon_whispers")
    if top["schema_version"] != SCHEMA_VERSION:
        raise DragonWhispersError("unsupported Dragon Whispers schema")
    generated_at = _timestamp(top["generated_at"], "generated_at")
    if top["status"] not in {"AWAITING_REVIEW", "REVIEWED_SIGNALS"}:
        raise DragonWhispersError("invalid Dragon Whispers status")
    if top["relation"] != RELATION:
        raise DragonWhispersError("Dragon Whispers relation was strengthened")
    _safe_text(top["input_provenance"], "input_provenance", maximum=500)
    _safe_text(top["method"], "method", maximum=500)
    _safe_text(top["scope"], "scope", maximum=500)
    policy = _exact(
        top["publication_policy"],
        {
            "human_review_required", "raw_messages_included",
            "source_identifiers_included", "exact_iocs_included",
            "named_allegations_included", "counts_as_corroboration",
        },
        "publication_policy",
    )
    if policy != {
        "human_review_required": True,
        "raw_messages_included": False,
        "source_identifiers_included": False,
        "exact_iocs_included": False,
        "named_allegations_included": False,
        "counts_as_corroboration": False,
    }:
        raise DragonWhispersError("publication policy broadens the public boundary")
    entries = top["entries"]
    if type(entries) is not list or len(entries) > 10_000:
        raise DragonWhispersError("entries must be a bounded array")
    if type(top["n_entries"]) is not int or top["n_entries"] != len(entries):
        raise DragonWhispersError("n_entries does not match entries")
    if (top["status"] == "REVIEWED_SIGNALS") != bool(entries):
        raise DragonWhispersError("status does not match reviewed entry availability")

    seen_ids: set[str] = set()
    seen_capsules: set[str] = set()
    ordering: list[tuple[str, str]] = []
    for index, raw_entry in enumerate(entries):
        path = f"entries[{index}]"
        entry = _exact(raw_entry, _ENTRY_FIELDS, path)
        whisper_id = entry["whisper_id"]
        if type(whisper_id) is not str or not _WHISPER_ID.fullmatch(whisper_id):
            raise DragonWhispersError(f"{path}.whisper_id is invalid")
        if whisper_id in seen_ids:
            raise DragonWhispersError("duplicate whisper_id")
        seen_ids.add(whisper_id)
        observed_at = _timestamp(entry["observed_at"], f"{path}.observed_at")
        published_at = _timestamp(entry["published_at"], f"{path}.published_at")
        if _parse_timestamp(published_at) < _parse_timestamp(observed_at):
            raise DragonWhispersError(f"{path} is published before observation")
        if _parse_timestamp(generated_at) < _parse_timestamp(published_at):
            raise DragonWhispersError("generated_at predates a published whisper")
        ordering.append((published_at, whisper_id))

        review = _exact(
            entry["review"],
            {
                "status", "reviewed_at", "reviewer_role",
                "source_capsule_sha256", "note",
            },
            f"{path}.review",
        )
        if review["status"] != "HUMAN_REVIEWED":
            raise DragonWhispersError(f"{path} lacks human review")
        if _timestamp(review["reviewed_at"], f"{path}.review.reviewed_at") != published_at:
            raise DragonWhispersError(f"{path} review and publication clocks differ")
        if type(review["reviewer_role"]) is not str or not _ROLE.fullmatch(
            review["reviewer_role"]
        ):
            raise DragonWhispersError(f"{path}.review.reviewer_role is invalid")
        capsule = review["source_capsule_sha256"]
        if type(capsule) is not str or not _SHA256.fullmatch(capsule):
            raise DragonWhispersError(f"{path}.review source receipt is invalid")
        if capsule in seen_capsules:
            raise DragonWhispersError("one source capsule produced multiple public whispers")
        seen_capsules.add(capsule)
        _safe_text(review["note"], f"{path}.review.note", maximum=500)

        signal = _exact(
            entry["signal"],
            {"tier", "families", "ioc_counts", "script_hints"},
            f"{path}.signal",
        )
        if signal["tier"] not in TIERS:
            raise DragonWhispersError(f"{path}.signal.tier is invalid")
        families = signal["families"]
        if type(families) is not list or len(families) > 32:
            raise DragonWhispersError(f"{path}.signal.families is invalid")
        if families != sorted(set(families)):
            raise DragonWhispersError(f"{path}.signal.families must be unique and sorted")
        for family in families:
            if type(family) is not str or not _LABEL.fullmatch(family):
                raise DragonWhispersError(f"{path}.signal contains an invalid family")
        ioc_counts = signal["ioc_counts"]
        if type(ioc_counts) is not dict or set(ioc_counts) - IOC_KINDS:
            raise DragonWhispersError(f"{path}.signal.ioc_counts is invalid")
        for kind, count in ioc_counts.items():
            _count(count, f"{path}.signal.ioc_counts.{kind}")
        hints = signal["script_hints"]
        if (
            type(hints) is not list
            or len(hints) > 8
            or hints != sorted(set(hints))
            or any(item not in SCRIPT_HINTS for item in hints)
        ):
            raise DragonWhispersError(f"{path}.signal.script_hints is invalid")

        analysis = _exact(
            entry["analysis"],
            {"headline", "summary", "why_it_matters", "uncertainty", "next_checks"},
            f"{path}.analysis",
        )
        _safe_text(analysis["headline"], f"{path}.analysis.headline", maximum=180)
        _safe_text(analysis["summary"], f"{path}.analysis.summary", maximum=1200)
        _safe_text(
            analysis["why_it_matters"], f"{path}.analysis.why_it_matters", maximum=1800
        )
        _safe_text(
            analysis["uncertainty"], f"{path}.analysis.uncertainty", maximum=1200
        )
        checks = analysis["next_checks"]
        if type(checks) is not list or not 2 <= len(checks) <= 8:
            raise DragonWhispersError(f"{path}.analysis.next_checks is invalid")
        for check_index, check in enumerate(checks):
            _safe_text(
                check,
                f"{path}.analysis.next_checks[{check_index}]",
                maximum=500,
            )

        limitations = entry["limitations"]
        if type(limitations) is not list or not 3 <= len(limitations) <= 8:
            raise DragonWhispersError(f"{path}.limitations must contain 3 to 8 items")
        for limitation_index, limitation in enumerate(limitations):
            _safe_text(
                limitation,
                f"{path}.limitations[{limitation_index}]",
                maximum=500,
            )

    if ordering != sorted(ordering, reverse=True):
        raise DragonWhispersError("entries are not newest-first")

    def scan_fields(node: Any, path: str = "dragon_whispers") -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).casefold() in _FORBIDDEN_FIELDS:
                    raise DragonWhispersError(f"{path} contains forbidden field {key!r}")
                scan_fields(value, f"{path}.{key}")
        elif isinstance(node, list):
            for item_index, value in enumerate(node):
                scan_fields(value, f"{path}[{item_index}]")

    scan_fields(document)
    canonical_json_bytes(document)


def whisper_id(source_capsule_sha256: str, published_at: str) -> str:
    if not _SHA256.fullmatch(source_capsule_sha256):
        raise DragonWhispersError("source capsule digest is invalid")
    _timestamp(published_at, "published_at")
    digest = hashlib.sha256(
        f"{source_capsule_sha256}\0{published_at}".encode("ascii")
    ).hexdigest()
    return f"whisper-{digest[:24]}"


__all__ = [
    "DragonWhispersError",
    "IOC_KINDS",
    "RELATION",
    "SCHEMA_VERSION",
    "SCRIPT_HINTS",
    "TIERS",
    "canonical_json_bytes",
    "empty_document",
    "validate_dragon_whispers",
    "whisper_id",
]
